from __future__ import annotations

import asyncio
import inspect
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from starlette.requests import Request
from starlette.responses import Response

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
    """The player spans both columns at its natural ratio, fitted like YouTube's theater."""

    def test_the_player_fits_the_viewport_with_room_for_the_title(self) -> None:
        css = main._home_styles()
        self.assertIn("body.theater .watch-player-shell { width: min(100%, calc((100vh - var(--header) - 120px) * 16 / 9));", css)
        self.assertIn("height: auto; aspect-ratio: 16 / 9;", css)
        # The old height cap letterboxed the video with side bars to keep the title
        # above the fold; fitting the width to the viewport keeps the ratio without bars.
        self.assertNotIn("--theater-reserve", css)
        self.assertNotIn("100cqw * 9 / 16", css)

    def test_the_stage_does_not_need_to_be_a_size_container(self) -> None:
        # container-type only existed to resolve the 100cqw height cap.
        self.assertNotIn("body.theater .watch-stage { container-type: inline-size;", main._home_styles())

    def test_the_player_meets_the_header_with_no_gap(self) -> None:
        # The page's usual 20px breathing room below the header would read as a seam.
        self.assertIn("body.theater .watch-main { padding-top: var(--header); }", main._home_styles())

    def test_the_title_does_not_stack_its_margin_on_the_stage(self) -> None:
        # Left as-is it both doubled the gap under the player and pushed the title 16px
        # below the up-next heading.
        self.assertIn("body.theater .watch-title { margin-top: 0; }", main._home_styles())

    def test_the_stage_stays_inside_the_gutter(self) -> None:
        # Full-bleed ran to the window edges and felt like a takeover; theater spans
        # both columns but keeps the page margins, radius and border.
        css = main._home_styles()
        self.assertIn("body.theater .watch-stage { background: transparent;", css)
        self.assertNotIn("margin: 0 calc(var(--watch-gutter) * -1) 14px;", css)
        shell = css.split("body.theater .watch-player-shell {", 1)[1].split("}", 1)[0]
        self.assertIn("border-radius: 0", shell)
        self.assertNotIn("border-left: 0", shell)

    def test_the_player_width_is_pinned(self) -> None:
        css = main._home_styles()
        self.assertIn("body.theater .watch-player-shell { width: min(100%, calc((100vh - var(--header) - 120px) * 16 / 9));", css)

    def test_the_theater_corners_do_not_eat_the_video(self) -> None:
        # Any round on the theater player visibly crops the video corners, so it is
        # square like YouTube's theater instead of carrying the card radius.
        css = main._home_styles()
        shell = css.split("body.theater .watch-player-shell {", 1)[1].split("}", 1)[0]
        self.assertIn("border-radius: 0;", shell)

    def test_the_iframe_fills_the_shell(self) -> None:
        # Height comes from the shell's ratio, not a second ratio on the frame —
        # two competing aspect-ratios stuttered on resize.
        self.assertIn("body.theater .player-frame { width: 100%; height: 100%; }", main._home_styles())

    def test_theater_does_not_transition_its_size(self) -> None:
        # The transition exists for sidebar open/close elsewhere; in theater the
        # shell tracks the window live, so animating it only rubber-bands.
        css = main._home_styles()
        self.assertIn("body.nav-ready:not(.theater) .watch-player-shell { transition:", css)
        self.assertNotIn("body.nav-ready .watch-player-shell { transition:", css)

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
        # No feed_entries row, so only the channel-level column is populated. This is
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
        self.assertIn("LEFT JOIN {user_feeds_sql()} cs ON cs.youtube_channel_id = c.youtube_channel_id", source)
        self.assertIn("cs.feed_url AS channel_subscription_feed_url", source)

    def test_subscription_columns_are_the_viewers_own(self) -> None:
        from openfeed.tests.sqlcapture import _capture_sql

        query, params = _capture_sql(lambda: db.get_video_by_youtube_id("vid", "alice"))
        self.assertEqual(params, ("alice", "alice", "vid"))
        self.assertEqual(query.count("us.user_key = %s"), 2)
        # No viewer, no subscription: the join matches nobody's follows.
        _, params = _capture_sql(lambda: db.get_video_by_youtube_id("vid"))
        self.assertEqual(params, (None, None, "vid"))


class SubscribeLocalTests(unittest.TestCase):
    """Subscribing upserts the shared feed and the caller's follow; unsubscribing removes
    only the caller's follow."""

    _STORED = {"feed_id": "feed/x", "created": True}

    def _subscribe(self, user_key: str = "u1", **overrides) -> dict[str, object]:
        params = {"youtube_channel_id": "UC1", "title": "Chan", "feed_url": "https://feed"}
        params.update(overrides)
        payload = main.SubscribeRequest(**params)
        with patch.object(main, "_user_key", return_value=user_key), \
             patch.object(main.db, "subscribe", return_value=self._STORED), \
             patch.object(main.db, "upsert_channel"), \
             patch.object(main, "_fetch_single_feed_best_effort"), \
             patch.object(main, "enrich_channel"):
            return main.api_subscribe(None, payload)

    def test_subscribe_creates_a_follow_for_the_caller_and_succeeds(self) -> None:
        with patch.object(main, "_user_key", return_value="alice"), \
             patch.object(main.db, "subscribe", return_value=self._STORED) as subscribed, \
             patch.object(main.db, "upsert_channel"), \
             patch.object(main, "_fetch_single_feed_best_effort"), \
             patch.object(main, "enrich_channel"):
            result = main.api_subscribe(
                None, main.SubscribeRequest(youtube_channel_id="UC1", title="Chan", feed_url="https://feed")
            )
        self.assertTrue(result["ok"])
        self.assertEqual(result["feed_url"], "https://feed")
        self.assertEqual(set(result), {"ok", "feed_url", "youtube_channel_id", "title"})
        subscribed.assert_called_once()
        self.assertEqual(subscribed.call_args.args[0], "alice")

    def test_subscribing_twice_is_idempotent(self) -> None:
        self.assertTrue(self._subscribe()["ok"])
        self.assertTrue(self._subscribe()["ok"])

    def test_subscribe_without_a_feed_or_channel_is_a_400(self) -> None:
        with self.assertRaises(main.HTTPException) as caught:
            self._subscribe(youtube_channel_id="", feed_url="", title="")
        self.assertEqual(caught.exception.status_code, 400)

    def test_subscribe_with_a_garbage_bare_channel_id_is_a_400(self) -> None:
        # A bare id mints the feed URL by itself, so a malformed one must fail
        # here instead of creating a junk row. A bare feed URL still goes
        # through (OPML/Takeout/non-YouTube feeds) and fails best-effort.
        with self.assertRaises(main.HTTPException) as caught:
            self._subscribe(youtube_channel_id="NOTAREALID", feed_url="", title="")
        self.assertEqual(caught.exception.status_code, 400)

    def test_subscribe_canonicalizes_url_variants_to_one_row(self) -> None:
        channel = "UC" + "b" * 22
        with patch.object(main, "_user_key", return_value="u1"), \
             patch.object(main.db, "subscribe", return_value=self._STORED), \
             patch.object(main.db, "upsert_channel"), \
             patch.object(main, "_fetch_single_feed_best_effort"), \
             patch.object(main, "enrich_channel"):
            first = main.api_subscribe(
                None, main.SubscribeRequest(youtube_channel_id=channel, title="C", feed_url="http://www.youtube.com/feeds/videos.xml?channel_id=" + channel + "/")
            )
            second = main.api_subscribe(
                None, main.SubscribeRequest(youtube_channel_id="", title="C", feed_url="https://www.youtube.com/feeds/videos.xml?channel_id=" + channel)
            )
        self.assertEqual(first["feed_url"], second["feed_url"])
        self.assertEqual(
            first["feed_url"], f"https://www.youtube.com/feeds/videos.xml?channel_id={channel}"
        )

    def test_unsubscribe_removes_only_the_callers_follow(self) -> None:
        payload = main.SubscribeRequest(youtube_channel_id="UC1", title="Chan", feed_url="https://feed")
        with patch.object(main, "_user_key", return_value="bob"), \
             patch.object(main.db, "remove_subscription", return_value=1) as removed:
            result = main.api_unsubscribe(None, payload)
        removed.assert_called_once_with("https://feed", "UC1", user_key="bob")
        self.assertTrue(result["ok"])
        self.assertTrue(result["removed_local"])

    def test_unsubscribe_without_a_feed_or_channel_is_a_400(self) -> None:
        payload = main.SubscribeRequest(youtube_channel_id="", title="", feed_url="")
        with patch.object(main, "_user_key", return_value="u1"), self.assertRaises(main.HTTPException) as caught:
            main.api_unsubscribe(None, payload)
        self.assertEqual(caught.exception.status_code, 400)

    def test_unsubscribe_of_a_missing_row_is_a_404(self) -> None:
        payload = main.SubscribeRequest(youtube_channel_id="UC1", title="Chan", feed_url="https://feed")
        with patch.object(main, "_user_key", return_value="u1"), \
             patch.object(main.db, "remove_subscription", return_value=0):
            with self.assertRaises(main.HTTPException) as caught:
                main.api_unsubscribe(None, payload)
        self.assertEqual(caught.exception.status_code, 404)

    def test_the_routes_are_the_plain_subscriptions_path(self) -> None:
        routes = {(method, route.path) for route in main.app.routes for method in getattr(route, "methods", set())}
        for method in ("GET", "POST", "DELETE"):
            self.assertIn((method, "/api/subscriptions"), routes)
        self.assertFalse(any(path.startswith("/api/subscriptions/") and path != "/api/subscriptions/bulk" for _, path in routes))


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

    def test_one_channel_cannot_fill_the_rail(self) -> None:
        # A pool with real variety: the cap holds and the rail still fills.
        pool = (
            [{"youtube_video_id": f"same{i}", "title": "same channel video",
              "duration_seconds": 900, "tags": ["synth"], "youtube_channel_id": "UC_anchor"}
             for i in range(6)]
            + [{"youtube_video_id": f"other{i}", "title": "other channel video",
                "duration_seconds": 900, "tags": [], "youtube_channel_id": f"UC_other{i}"}
               for i in range(20)]
        )
        out = self._related(pool)
        self.assertEqual(sum(1 for vid in out if vid.startswith("same")), 2)
        self.assertEqual(len(out), 14)

    def test_a_narrow_pool_still_fills_the_rail(self) -> None:
        # Backfill, not empty slots, when one channel genuinely dominates the pool.
        pool = [
            {"youtube_video_id": f"same{i}", "title": "same channel video",
             "duration_seconds": 900, "tags": ["synth"], "youtube_channel_id": "UC_anchor"}
            for i in range(6)
        ]
        self.assertEqual(len(self._related(pool)), 6)

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


class EmbedChromeTests(unittest.TestCase):
    """The Invidious player is restyled toward YouTube inside our own embed."""

    def test_the_video_fills_the_frame_instead_of_pillarboxing(self) -> None:
        # Invidious sizes its fluid player to 82vh, which bars every video inside
        # our exactly-16:9 shell; the ratio override makes it reach the edges.
        script = main._player_script()
        self.assertIn(".player-dimensions.vjs-fluid, .video-js.vjs-fluid { padding-top: 56.25% !important; }", script)

    def test_the_chrome_is_a_flat_scrim(self) -> None:
        # OpenFeed's chrome is one flat scrim. A gradient behind the controls is out of
        # system, and the solid stock bar read as the whole video dimming.
        script = main._player_script()
        self.assertIn(".video-js .vjs-control-bar { background: var(--of-scrim) !important;", script)
        self.assertIn(".video-js .vjs-overlay { background: var(--of-scrim) !important;", script)
        self.assertNotIn("linear-gradient(transparent", script)

    def test_the_scrubber_is_ember_over_dry_slate(self) -> None:
        css = main._player_chrome_css()
        self.assertIn("background: var(--of-ember) !important;", css)
        self.assertIn("background: var(--of-slate) !important;", css)
        self.assertIn(".vjs-progress-control:hover .vjs-progress-holder", css)

    def test_the_big_play_button_is_centred_and_not_a_colour_fill(self) -> None:
        # video.js parks it top left, which is the grey box testers kept reporting.
        css = main._player_chrome_css()
        self.assertIn("top: 50%; left: 50%; width: 64px; height: 64px;", css)
        self.assertIn(".video-js .vjs-big-play-button, .video-js:hover .vjs-big-play-button { background-color: var(--of-scrim) !important; }", css)

    def test_the_chrome_survives_being_spliced_into_a_template_literal(self) -> None:
        # _player_script() pastes this into a JavaScript template literal.
        css = main._player_chrome_css()
        for forbidden in ("`", "\\", "${"):
            self.assertNotIn(forbidden, css)
        self.assertNotIn("__OPENFEED_PLAYER_CHROME__", main._player_script())

    def test_stock_video_js_never_shows_while_the_style_is_on_its_way(self) -> None:
        script = main._player_script()
        self.assertIn("tipify(doc);", script)
        self.assertIn("data-tip", main._native_player_page("/x", "Downloaded", True).body.decode())


class NativePlayerTests(unittest.TestCase):
    """/local and /ytdlp wear the same chrome, without needing Invidious to be up."""

    def _page(self, autoplay: bool = True) -> str:
        return main._native_player_page("/api/ytdlp/stream/abc#t=0", "Direct stream &middot; 720p", autoplay).body.decode()

    def test_it_uses_the_shared_stylesheet(self) -> None:
        self.assertIn(main._player_chrome_css(), self._page())

    def test_it_borrows_nothing_from_behind_invidious(self) -> None:
        self.assertNotIn("/videojs/", self._page())

    def test_native_controls_stay_until_the_script_replaces_them(self) -> None:
        self.assertIn("<video controls playsinline", self._page())
        self.assertIn("video.controls = false;", self._page())

    def test_autoplay_is_only_set_when_asked_for(self) -> None:
        self.assertIn(" autoplay src=", self._page(True))
        self.assertNotIn(" autoplay src=", self._page(False))

    def test_the_style_guard_is_already_satisfied(self) -> None:
        # The shell injects the chrome unless this id is present, so it is never doubled.
        self.assertIn('<style id="openfeed-embed-style">', self._page())


class PlayerCspTests(unittest.TestCase):
    """Part of the security hardening: only the embed gets its two extra sources."""

    BASE = (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; media-src 'self' blob:; font-src 'self'; connect-src 'self'; "
        "frame-src 'self'; worker-src 'self'; manifest-src 'self'; object-src 'none'; base-uri 'none'; "
        "form-action 'self'; frame-ancestors 'self'"
    )

    def _csp(self, path: str) -> str:
        scope = {"type": "http", "method": "GET", "path": path, "query_string": b"", "headers": [],
                 "scheme": "http", "server": ("testserver", 80)}

        async def call_next(_request: Request) -> Response:
            return Response()

        response = asyncio.run(main.harden_http(Request(scope), call_next))
        return response.headers["Content-Security-Policy"]

    def test_the_embed_gets_exactly_the_font_and_worker_it_needs(self) -> None:
        expected = self.BASE.replace("font-src 'self';", "font-src 'self' data:;").replace("worker-src 'self';", "worker-src 'self' blob:;")
        self.assertEqual(self._csp("/embed/abc"), expected)

    def test_every_other_page_keeps_the_base_policy(self) -> None:
        for path in ("/", "/watch/abc", "/watch/abc/player", "/watch/abc/local", "/watch/abc/ytdlp", "/shorts", "/embed"):
            self.assertEqual(self._csp(path), self.BASE, path)

    def test_the_api_stays_unframeable(self) -> None:
        self.assertEqual(self._csp("/api/playback/sources"), "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")


class PlaybackFailureCardTests(unittest.TestCase):
    """Fail loudly and name the cause: the card is built from the ladder and the walk."""

    def test_a_blocked_frame_is_a_failure_not_a_healthy_start(self) -> None:
        script = main._player_script()
        self.assertIn("securitypolicyviolation", script)
        self.assertIn("reported.delete(`${source.id}:true`)", script)

    def test_an_unreadable_same_origin_frame_moves_on(self) -> None:
        self.assertIn("if (!watchSameOrigin()) next('frame error');", main._player_script())

    def test_the_cause_comes_from_the_real_diagnosis(self) -> None:
        script = main._player_script()
        self.assertIn("/api/playback/diagnose?video_id=", script)
        self.assertIn("diagnosis.headline", script)

    def test_every_action_is_wired(self) -> None:
        script = main._player_script()
        for name in ("retry", "next", "diagnostics", "copy"):
            self.assertIn(f"part('{name}').addEventListener", script)

    def test_a_recovering_player_is_not_reported_as_failed(self) -> None:
        # Invidious's dash player retries by itself; only a lasting error opens the card.
        self.assertIn("RECOVERY_GRACE", main._player_script())

    def test_the_report_text_keeps_its_newline_escape(self) -> None:
        # A bare newline here is a syntax error in the shipped script, and the player dies.
        self.assertIn("lines.join('\\n')", main._player_script())


class ChapterTests(unittest.TestCase):
    LIST = "Intro\n0:00 Start\n1:23 Middle part\n12:34 - The end\nThanks for watching 99:99"

    def test_a_plain_list_parses(self) -> None:
        self.assertEqual(
            [(c["start"], c["title"]) for c in main._parse_chapters(self.LIST)],
            [(0, "Start"), (83, "Middle part"), (754, "The end")],
        )

    def test_the_range_form_parses(self) -> None:
        text = "00:00:00-00:17:13 Eagles\n00:17:14-00:26:22 Jayden Reed\n00:26:23-00:41:33 Breaking"
        chapters = main._parse_chapters(text)
        self.assertEqual([c["start"] for c in chapters], [0, 1034, 1583])
        self.assertEqual(chapters[1]["title"], "Jayden Reed")

    def test_timestamps_in_prose_are_not_chapters(self) -> None:
        self.assertEqual(main._parse_chapters("We air live at 4:00 pm\nand again at 11:15 pm"), [])

    def test_a_list_must_start_at_zero(self) -> None:
        self.assertEqual(main._parse_chapters("1:00 One\n2:00 Two\n3:00 Three"), [])

    def test_two_entries_are_not_chapters(self) -> None:
        self.assertEqual(main._parse_chapters("0:00 One\n2:00 Two"), [])

    def test_entries_closer_than_ten_seconds_are_dropped(self) -> None:
        chapters = main._parse_chapters("0:00 One\n0:05 Too soon\n1:00 Two\n2:00 Three")
        self.assertEqual([c["title"] for c in chapters], ["One", "Two", "Three"])

    def test_entries_past_the_end_of_the_video_are_dropped(self) -> None:
        chapters = main._parse_chapters("0:00 One\n1:00 Two\n2:00 Three\n9:00 Four", 300)
        self.assertEqual(len(chapters), 3)

    def test_titles_are_escaped_in_the_panel(self) -> None:
        html = main._chapter_card([{"start": 0, "label": "0:00", "title": "<img src=x onerror=alert(1)>"}])
        self.assertNotIn("<img", html)
        self.assertIn("&lt;img", html)

    def test_no_chapters_means_no_card(self) -> None:
        self.assertEqual(main._chapter_card([]), "")

    def test_rows_use_the_shared_seek_hook(self) -> None:
        html = main._chapter_card([{"start": 83, "label": "1:23", "title": "Middle"}])
        self.assertIn('data-seek="83"', html)
        self.assertIn('data-chapter-start="83"', html)


class CommentSortTests(unittest.TestCase):
    def test_newest_is_the_sources_own_sort(self) -> None:
        source = inspect.getsource(main.api_comments)
        self.assertIn('params["sort_by"] = "new"', source)
        self.assertIn('if sort == "new":', source)

    def test_timed_says_it_only_covers_what_has_loaded(self) -> None:
        self.assertIn("loaded comments name a time in the video", main._home_script())

    def test_there_is_no_composer(self) -> None:
        source = inspect.getsource(main.watch_video)
        self.assertNotIn("<textarea", source)


class WatchActionRowTests(unittest.TestCase):
    def test_the_theater_row_has_a_clean_name(self) -> None:
        # The shortcut hint is its own span, so the accessible name read "Theater modet".
        html = main._watch_options_menu("vid1", "", "https://yt")
        self.assertIn('aria-label="Theater mode"', html)
        self.assertIn('aria-keyshortcuts="t"', html)

    def test_icon_only_actions_are_labelled(self) -> None:
        source = inspect.getsource(main.watch_video)
        self.assertIn('data-share-open aria-haspopup="dialog" aria-label="Share" data-tip="Share"', source)
        self.assertIn('aria-label="Save to playlist" data-tip="Save"', source)


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
    def test_status_is_a_settings_pane_and_a_shareable_page(self) -> None:
        import inspect

        from openfeed.tests.support import offline_db

        # /admin/status stays on its own URL instead of redirecting to a dialog over the
        # feed, and both surfaces load the same panel.
        source = inspect.getsource(main.admin_status)
        self.assertNotIn("RedirectResponse", source)
        self.assertIn("data-status-autoload", source)
        with offline_db():
            self.assertIn('data-settings-pane="status"', main._settings_dialog(user={"authenticated": False}))

    def test_the_status_body_still_renders(self) -> None:
        from openfeed.tests.support import offline_db

        with offline_db():
            html = main._status_page_html("local")
        for section in ("status-verdict", "status-counters", "Source ladder", "Hop walk", "Services", "Raw status output"):
            self.assertIn(section, html)

    def test_the_verdict_names_the_cause_and_comes_only_from_measurements(self) -> None:
        healthy = {"ok": True}
        status = {"checks": {key: dict(healthy) for key in main._STATUS_CHECK_LABELS}}
        source = {"id": "invidious", "label": "Invidious", "enabled": True, "cooling": False, "failing": False, "health": {"status": "healthy"}}
        fallback = {"id": "youtube", "label": "YouTube", "enabled": True, "cooling": False, "failing": False, "health": {"status": "unknown"}}

        self.assertEqual(main._status_verdict(status, [source, fallback])[:2], ("ok", "Playback healthy via Invidious"))
        # Never tried: it must not claim health nobody observed.
        tone, headline, detail = main._status_verdict(status, [dict(source, health={"status": "unknown"})])
        self.assertEqual(tone, "ok")
        self.assertNotIn("healthy", headline)
        self.assertIn("not played", detail)

        tone, headline, _ = main._status_verdict(status, [dict(source, cooling=True), fallback])
        self.assertEqual((tone, headline), ("warn", "Playback degraded: Invidious is paused, using YouTube"))
        self.assertEqual(main._status_verdict(status, [dict(source, enabled=False)])[0], "bad")
        self.assertEqual(main._status_verdict(status, [dict(source, cooling=True)])[0], "bad")

        status["checks"]["database"] = {"ok": False, "error": "connection refused"}
        self.assertEqual(main._status_verdict(status, [source])[:2], ("bad", "Postgres is failing: connection refused"))

    def test_the_ladder_inverts_the_source_a_play_would_use_and_shows_the_cooldown(self) -> None:
        cooling = {"id": "a", "label": "First", "enabled": True, "cooling": True, "failing": False, "health": {"cooldown_seconds": 29, "failures": 2}}
        ready = {"id": "b", "label": "Second", "enabled": True, "cooling": False, "failing": False, "health": {"status": "healthy", "last_ok": 4}}
        html = main._status_ladder([cooling, ready])
        self.assertIn('data-cooldown="29"', html)
        self.assertIn("2 recent failures", html)
        self.assertLess(html.index("Second"), html.index("Active"))
        self.assertEqual(html.count("Active"), 1)

    def test_status_loading_is_a_skeleton_not_text(self) -> None:
        self.assertIn("skeleton", main._status_skeleton())
        self.assertNotIn("Loading status&hellip;", main._home_script())

    def test_status_tables_scroll_in_their_own_box(self) -> None:
        # A long error message must not be able to widen the whole page.
        self.assertIn(".status-scroll { overflow-x: auto;", main._home_styles())


class FaviconTests(unittest.TestCase):
    """The app served no icon at all, so every tab was blank."""

    def test_the_asset_map_is_explicit_rather_than_a_directory(self) -> None:
        # A directory mount would put a URL-supplied name on the filesystem. This maps
        # names to media types, so there is nothing to traverse.
        self.assertIn("favicon-32.png", main._STATIC_ASSETS)
        self.assertIn("apple-touch-icon.png", main._STATIC_ASSETS)

    def test_an_unknown_name_is_a_404_not_a_file_read(self) -> None:
        from fastapi import HTTPException

        for name in ("../config.py", "../../etc/passwd", "nope.png"):
            with self.assertRaises(HTTPException) as caught:
                main.static_asset(name)
            self.assertEqual(caught.exception.status_code, 404)

    def test_the_icons_ship_inside_the_package(self) -> None:
        # The Dockerfile copies only `openfeed`, so an asset outside it would 404 in the
        # container while working fine from a source checkout.
        for name in main._STATIC_ASSETS:
            self.assertTrue((main._ASSET_DIR / name).is_file(), f"{name} missing from the package")

    def test_favicon_ico_is_answered(self) -> None:
        # Browsers request /favicon.ico regardless of the markup.
        self.assertEqual(main.favicon().status_code, 200)

    def test_every_page_template_gets_the_icon(self) -> None:
        # Adding the markup to one head is how tabs stayed blank everywhere else: the app
        # shell, the standalone pages and the error page each build their own <head>.
        import re

        source = inspect.getsource(main)
        heads = len(re.findall(r"<head>", source)) + source.count("<head><meta charset")
        icons = source.count("_head_icons()")
        # One definition plus one call per user-facing template; the player iframes are
        # deliberately excluded, so calls must be fewer than heads but more than one.
        self.assertGreater(icons, 2, "not every page template calls _head_icons()")
        self.assertLess(icons, heads + 2)

    def test_the_player_iframe_does_not_carry_one(self) -> None:
        # Nothing displays a favicon for an embedded frame.
        self.assertNotIn("_head_icons", inspect.getsource(main.watch_player))


class PublishedDateDecodedTests(unittest.TestCase):
    """List endpoints report "2 days ago" as the fetch wall-clock; the single-video
    endpoint reports a real date at midnight UTC. That difference is the signature."""

    def test_midnight_utc_is_a_real_date(self) -> None:
        from datetime import datetime, timezone

        self.assertFalse(main._published_date_is_decoded(datetime(2026, 9, 1, tzinfo=timezone.utc)))

    def test_a_wall_clock_time_is_decoded(self) -> None:
        from datetime import datetime, timezone

        self.assertTrue(main._published_date_is_decoded(datetime(2026, 9, 1, 15, 30, tzinfo=timezone.utc)))

    def test_missing_and_naive_values_do_not_crash(self) -> None:
        from datetime import datetime

        self.assertFalse(main._published_date_is_decoded(None))
        self.assertFalse(main._published_date_is_decoded(datetime(2026, 9, 1, 0, 0)))
        self.assertTrue(main._published_date_is_decoded(datetime(2026, 9, 1, 12, 0)))


class VideoDetailsEndpointTests(unittest.TestCase):
    """The watch page polls this for details that arrive after first paint."""

    COMPLETE = {
        "youtube_video_id": "vid1", "title": "A video", "view_count": 138,
        "summary": "hello https://example.com", "like_count": 6,
        "published_at": __import__("datetime").datetime(2026, 9, 1, tzinfo=__import__("datetime").timezone.utc),
    }

    def _details(self, row: dict) -> dict:
        with patch.object(main, "_user_key", return_value="local"), \
             patch.object(db, "get_video_by_youtube_id", return_value=row):
            return main.api_video_details(None, row["youtube_video_id"])

    def test_a_complete_row_reports_complete_with_rendered_markup(self) -> None:
        out = self._details(dict(self.COMPLETE))
        self.assertTrue(out["complete"])
        self.assertTrue(out["has_description"])
        self.assertIn("138 views", out["meta_html"])
        self.assertIn("https://example.com", out["body_html"])

    def test_a_pending_row_is_incomplete_with_no_body(self) -> None:
        row = dict(self.COMPLETE, summary="", description="")
        out = self._details(row)
        self.assertFalse(out["complete"])
        self.assertFalse(out["has_description"])
        self.assertEqual(out["body_html"], "")

    def test_a_decoded_date_keeps_it_pending(self) -> None:
        from datetime import datetime, timezone

        row = dict(self.COMPLETE, published_at=datetime(2026, 9, 15, 20, 0, tzinfo=timezone.utc))
        self.assertFalse(self._details(row)["complete"])


class ChannelVideosHtmlTests(unittest.TestCase):
    """Load-more has to continue the tab it was rendered from, not Latest."""

    def _row(self, youtube_video_id: str, duration: int) -> dict:
        return {"youtube_video_id": youtube_video_id, "title": youtube_video_id,
                "url": f"https://www.youtube.com/watch?v={youtube_video_id}",
                "duration_seconds": duration, "channel_title": "Chan"}

    def test_playlists_tab_returns_empty_and_never_queries(self) -> None:
        with patch.object(db, "list_channel_videos") as videos:
            out = main.api_channel_videos_html(None, "UC_x", kind="playlists")
        videos.assert_not_called()
        self.assertEqual(out, {"ok": True, "html": "", "count": 0, "fetched_remote": 0,
                               "next_offset": 0, "has_more": False})

    def test_search_query_reaches_the_library_query(self) -> None:
        with patch.object(db, "list_channel_videos", return_value=[]) as videos, \
             patch.object(main, "_ingest_channel_uploads", return_value=0):
            main.api_channel_videos_html(None, "UC_x", q="synth")
        self.assertEqual(videos.call_args[0][4], "synth")

    def test_shorts_tab_scans_a_window_and_filters(self) -> None:
        rows = [self._row(f"long{i}", 600) for i in range(71)] + [self._row("short1", 30)]
        with patch.object(db, "list_channel_videos", return_value=rows), \
             patch.object(main, "_ingest_channel_uploads", return_value=0):
            out = main.api_channel_videos_html(None, "UC_x", offset=0, limit=24, kind="shorts")
        self.assertEqual(out["count"], 1)
        self.assertIn("short1", out["html"])
        self.assertNotIn("long0", out["html"])
        # The whole 72-row window is consumed, so scrolling continues past it.
        self.assertEqual(out["next_offset"], 72)
        self.assertTrue(out["has_more"])


class SearchHtmlTests(unittest.TestCase):
    """Search scrolls through YouTube pages without repeating what is shown."""

    def _row(self, youtube_video_id: str) -> dict:
        return {"youtube_video_id": youtube_video_id, "title": youtube_video_id}

    def _fake_row(self, video: dict, settings: object, badge: str) -> str:
        return f'<article data-video-id="{video["youtube_video_id"]}">{badge}:{video["youtube_video_id"]}</article>'

    def _html(self, **kwargs) -> dict:
        from openfeed import discovery

        settings = SimpleNamespace()
        with patch.object(main, "get_settings", return_value=settings), \
             patch.object(main, "_media_row", side_effect=self._fake_row), \
             patch.object(db, "search_videos", return_value=kwargs.pop("local", [])) as local, \
             patch.object(discovery, "search_youtube", return_value=kwargs.pop("remote", [])) as remote, \
             patch.object(db, "store_search_results", return_value=0) as stored, \
             patch.object(db, "video_ids_in_library", return_value=kwargs.pop("member", set())) as membership:
            out = main.api_search_html(None, **kwargs)
        return {"out": out, "local": local, "remote": remote, "stored": stored, "membership": membership}

    def test_empty_query_returns_nothing_and_never_searches(self) -> None:
        from openfeed import discovery

        with patch.object(discovery, "search_youtube") as remote:
            out = main.api_search_html(None, q="   ")
        remote.assert_not_called()
        self.assertEqual(out, {"ok": True, "html": "", "count": 0, "next_page": 2, "has_more": False})

    def test_later_pages_are_youtube_only_and_skip_what_is_stored(self) -> None:
        seen = self._html(q="synth", page=2, local=[self._row("lib1")], member={"lib1", "page1"},
                          remote=[self._row("lib1"), self._row("page1"), self._row("new2")])
        seen["local"].assert_not_called()
        # The library side is page 1's job; page 2 only checks stored membership.
        seen["membership"].assert_called_once()
        out = seen["out"]
        self.assertNotIn("lib1", out["html"])
        self.assertNotIn("page1", out["html"])
        self.assertIn("From YouTube:new2", out["html"])
        self.assertEqual(out["count"], 1)
        self.assertEqual(out["next_page"], 3)
        self.assertTrue(out["has_more"])
        seen["stored"].assert_called_once()

    def test_last_page_never_reports_more(self) -> None:
        seen = self._html(q="synth", page=5, remote=[self._row("new5")])
        self.assertEqual(seen["out"]["next_page"], 6)
        self.assertFalse(seen["out"]["has_more"])

    def test_an_unreachable_youtube_side_stops_the_scroll(self) -> None:
        from openfeed import discovery

        settings = SimpleNamespace()
        with patch.object(main, "get_settings", return_value=settings), \
             patch.object(discovery, "search_youtube", side_effect=RuntimeError("down")):
            out = main.api_search_html(None, q="synth", page=2)
        self.assertEqual(out, {"ok": True, "html": "", "count": 0, "next_page": 2, "has_more": False})

    def test_page_one_mirrors_the_search_page(self) -> None:
        seen = self._html(q="synth", page=1, local=[self._row("lib1")],
                          remote=[self._row("lib1"), self._row("new1")])
        out = seen["out"]
        self.assertIn("In your library:lib1", out["html"])
        self.assertIn("From YouTube:new1", out["html"])
        self.assertEqual(out["count"], 2)
        self.assertTrue(out["has_more"])


class SearchPageTests(unittest.TestCase):
    """The search hero names the query; the counts line is gone."""

    def _content(self, q: str, remote: list | None = None, error: bool = False, channels: list | None = None,
                 suggestions: list | None = None, **filters: str) -> str:
        from openfeed import discovery

        rows = remote if remote is not None else [{"youtube_video_id": "new1", "title": "new1"}]
        search = RuntimeError("down") if error else None
        settings = SimpleNamespace()
        with patch.object(main, "_login_required_redirect", return_value=None), \
             patch.object(main, "get_settings", return_value=settings), \
             patch.object(db, "search_videos", return_value=[]), \
             patch.object(db, "list_recent_videos", return_value=[]), \
             patch.object(discovery, "search_youtube", return_value=rows, side_effect=None if not error else search), \
             patch.object(db, "store_search_results", return_value=0), \
             patch.object(main, "_search_channels", return_value=(channels or [], "", False)), \
             patch.object(main, "_search_suggestions", return_value=suggestions or []), \
             patch.object(main, "_notification_levels", return_value={}), \
             patch.object(main, "_user_key", return_value="local"), \
             patch.object(main, "_media_row", return_value='<article data-video-id="new1"></article>'), \
             patch.object(main, "_app_shell", return_value="") as shell:
            main.search_page(None, q=q, **filters)
        return shell.call_args[0][2]

    def test_heading_counts_and_quotes_the_query(self) -> None:
        content = self._content(" derezzed ")
        self.assertIn("1+ result for &quot;derezzed&quot;", content)
        self.assertNotIn("from your library", content)
        self.assertNotIn("from YouTube", content)

    def test_a_searchable_page_carries_the_scroll_hooks(self) -> None:
        content = self._content("synth")
        self.assertIn("data-search-list", content)
        self.assertIn('data-q="synth"', content)
        self.assertIn('data-next-page="2"', content)
        self.assertIn('data-search-ok="1"', content)
        self.assertIn("data-search-sentinel", content)
        self.assertIn("data-search-loading", content)

    def test_an_unreachable_youtube_side_has_no_sentinel(self) -> None:
        content = self._content("synth", error=True)
        self.assertIn('data-search-ok="0"', content)
        self.assertNotIn("data-search-sentinel", content)

    def test_recent_videos_have_no_sentinel(self) -> None:
        content = self._content("")
        self.assertNotIn("data-search-sentinel", content)

    def test_type_chips_are_plain_links_that_mark_the_active_type(self) -> None:
        content = self._content("synth", type="videos")
        self.assertIn('href="/search?q=synth&amp;type=channels"', content)
        self.assertIn('class="ch-chip active" href="/search?q=synth&amp;type=videos" aria-current="page"', content)

    def test_filters_survive_into_the_scroll_hooks(self) -> None:
        content = self._content("synth", sort="view_count", date="week", duration="long")
        self.assertIn('data-sort="view_count"', content)
        self.assertIn('data-date="week"', content)
        self.assertIn('data-duration="long"', content)
        self.assertIn("Clear filters", content)

    def test_filter_values_outside_the_tables_are_dropped(self) -> None:
        content = self._content("synth", sort="drop table", date="<script>", duration="forever", type="nope")
        self.assertIn('data-type="all"', content)
        self.assertIn('data-sort="relevance"', content)
        self.assertIn('data-date=""', content)
        self.assertNotIn("<script>", content)

    def test_shorts_and_playlists_only_offer_filters_that_apply(self) -> None:
        self.assertNotIn(">Length<", self._content("synth", type="shorts"))
        with patch.object(main, "_search_playlists", return_value=("", 0, "", False)):
            self.assertNotIn("filter-menu", self._content("synth", type="playlists"))

    def test_channels_lead_all_and_are_escaped(self) -> None:
        evil = '"><script>alert(1)</script>'
        content = self._content("synth", channels=[{"youtube_channel_id": "UCa", "title": evil}])
        self.assertLess(content.index("search-channels"), content.index("data-search-list"))
        self.assertNotIn(evil, content)
        self.assertIn('data-subscribe-feed', content)

    def test_a_filtered_search_has_no_channel_block(self) -> None:
        content = self._content("synth", channels=[{"youtube_channel_id": "UCa", "title": "A"}], date="week")
        self.assertNotIn("search-channels", content)

    def test_no_results_offers_suggestions_as_links(self) -> None:
        content = self._content("two words", remote=[], suggestions=["two"])
        self.assertIn("empty-state", content)
        self.assertIn('href="/search?q=two"', content)
        self.assertNotIn("data-search-sentinel", content)

    def test_suggestions_come_from_the_query(self) -> None:
        with patch.object(db, "list_subscriptions", return_value=[{"title": "Veritasium"}]):
            self.assertEqual(main._search_suggestions("a long query"), ["a long", "long query", "query", "long"])
            self.assertEqual(main._search_suggestions("veritasiumm", "u1"), ["Veritasium"])
            # Without a user there is no subscription list to suggest from.
            self.assertEqual(main._search_suggestions("veritasiumm"), [])

    def test_channels_found_on_youtube_are_stored_before_they_are_linked(self) -> None:
        from openfeed import discovery

        local = {"youtube_channel_id": "UCa", "title": "Mine"}
        found = [{"youtube_channel_id": "UCa", "title": "Mine"}, {"youtube_channel_id": "UCb", "title": "New"}]
        stored = [{"youtube_channel_id": "UCb", "title": "New"}]
        with patch.object(db, "search_channels", return_value=[local]), \
             patch.object(discovery, "search_youtube_channels", return_value=found), \
             patch.object(db, "store_search_channels", return_value=stored) as store:
            rows, error, more = main._search_channels("q", 5, 1, "u1")
        store.assert_called_once_with([found[1]], user_key="u1")
        self.assertEqual([row["youtube_channel_id"] for row in rows], ["UCa", "UCb"])
        self.assertEqual(error, "")
        self.assertTrue(more)


class SearchBackendTests(unittest.TestCase):
    """What the filters actually send, to Invidious and to the database."""

    def test_sort_uses_the_parameter_invidious_reads(self) -> None:
        from openfeed import discovery

        class Response:
            def raise_for_status(self) -> None:
                pass

            def json(self) -> list:
                return []

        class Client:
            sent: dict = {}

            def __init__(self, **kwargs) -> None:
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args) -> None:
                pass

            def get(self, url: str, params: dict) -> Response:
                Client.sent = params
                return Response()

        settings = SimpleNamespace(invidious_base_url="http://inv", enrichment_timeout_seconds=5)
        with patch.object(discovery, "get_settings", return_value=settings), \
             patch.object(discovery.httpx, "Client", Client):
            discovery.search_youtube("q", sort="view_count", date="week", duration="long")
        self.assertEqual(Client.sent["sort"], "views")
        self.assertNotIn("sort_by", Client.sent)
        self.assertEqual(Client.sent["date"], "week")
        self.assertEqual(Client.sent["duration"], "long")

    def test_a_short_result_is_kept_or_dropped_by_type(self) -> None:
        from openfeed import discovery

        rows = [{"youtube_video_id": "a", "duration_seconds": 40}, {"youtube_video_id": "b", "duration_seconds": 400}]
        with patch.object(discovery, "search_youtube", return_value=rows) as search:
            shorts, scanned = main._search_youtube_videos("q", 1, main._search_filters("shorts", "relevance", "", ""))
            videos, _ = main._search_youtube_videos("q", 1, main._search_filters("videos", "relevance", "", ""))
        self.assertEqual([row["youtube_video_id"] for row in shorts], ["a"])
        self.assertEqual([row["youtube_video_id"] for row in videos], ["b"])
        self.assertEqual(scanned, 2)
        self.assertEqual(search.call_args_list[0].kwargs["duration"], "short")

    def test_filter_sql_only_ever_comes_from_the_tables(self) -> None:
        clause = db._search_filter_sql("shorts", "week", "medium")
        self.assertIn("interval '7 days'", clause)
        self.assertIn("v.duration_seconds >= 240", clause)
        self.assertEqual(db._search_filter_sql("", "'; drop table videos;--", "nope"), "")


class CommentAvatarResolverTests(unittest.TestCase):
    """Invidious returns no comment avatars, so rows resolve them lazily as a batch."""

    def _avatars(self, ids: list) -> dict:
        settings = SimpleNamespace(invidious_base_url="http://inv", enrichment_timeout_seconds=5)
        with patch.object(main, "_login_required_redirect", return_value=None), \
             patch.object(main, "_user_key", return_value="local"), \
             patch.object(db, "get_feed_preferences", return_value={"show_comments": True}), \
             patch.object(main, "get_settings", return_value=settings):
            return main.api_comment_avatars(None, main.CommentAvatarsRequest(author_ids=ids))

    def test_non_channel_ids_never_reach_the_network(self) -> None:
        with patch.object(main, "_fetch_commenter_avatar") as fetch:
            out = self._avatars(["../../etc/passwd", "", "x", "has space", "a" * 65])
        fetch.assert_not_called()
        # Nothing valid was even considered; nothing cached, nothing returned.
        self.assertEqual(out, {"ok": True, "avatars": {}})

    def test_cache_hits_skip_the_network(self) -> None:
        main._comment_avatar_store("UC_cached", "/ggpht/abc")
        try:
            with patch.object(main, "_fetch_commenter_avatar") as fetch:
                out = self._avatars(["UC_cached"])
            fetch.assert_not_called()
            self.assertEqual(out["avatars"], {"UC_cached": "/ggpht/abc"})
        finally:
            main._COMMENT_AVATAR_CACHE.pop("UC_cached", None)

    def test_misses_are_fetched_stored_and_returned(self) -> None:
        main._COMMENT_AVATAR_CACHE.pop("UC_new", None)
        try:
            with patch.object(main, "_fetch_commenter_avatar", return_value="/ggpht/new") as fetch:
                out = self._avatars(["UC_new"])
            fetch.assert_called_once()
            self.assertEqual(out["avatars"], {"UC_new": "/ggpht/new"})
            hit, url = main._comment_avatar_cached("UC_new")
            self.assertTrue(hit)
            self.assertEqual(url, "/ggpht/new")
        finally:
            main._COMMENT_AVATAR_CACHE.pop("UC_new", None)

    def test_an_expired_miss_is_looked_up_again(self) -> None:
        main._COMMENT_AVATAR_CACHE["UC_retry"] = (0.0, "")
        try:
            with patch.object(main, "_fetch_commenter_avatar", return_value="/ggpht/r") as fetch:
                out = self._avatars(["UC_retry"])
            fetch.assert_called_once()
            self.assertEqual(out["avatars"], {"UC_retry": "/ggpht/r"})
        finally:
            main._COMMENT_AVATAR_CACHE.pop("UC_retry", None)

    def test_disabled_comments_are_forbidden(self) -> None:
        from fastapi import HTTPException

        settings = SimpleNamespace(invidious_base_url="http://inv", enrichment_timeout_seconds=5)
        with patch.object(main, "_login_required_redirect", return_value=None), \
             patch.object(main, "_user_key", return_value="local"), \
             patch.object(db, "get_feed_preferences", return_value={}), \
             patch.object(main, "get_settings", return_value=settings):
            with self.assertRaises(HTTPException) as caught:
                main.api_comment_avatars(None, main.CommentAvatarsRequest(author_ids=["UC_x"]))
        self.assertEqual(caught.exception.status_code, 403)
