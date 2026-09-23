from __future__ import annotations

import unittest
from unittest.mock import patch

from openfeed import enrichment, main


class DescriptionLinkTests(unittest.TestCase):
    """Links come from the description because Invidious exposes no link fields."""

    def test_a_labelled_line_keeps_its_label(self) -> None:
        links = main._description_links("IG: https://instagram.com/vanessastclair/?hl=en")
        self.assertEqual(links, [("IG", "https://instagram.com/vanessastclair/?hl=en")])

    def test_a_bare_url_falls_back_to_its_domain(self) -> None:
        links = main._description_links("https://vanessastclair.com/")
        self.assertEqual(links, [("vanessastclair.com", "https://vanessastclair.com/")])

    def test_prose_is_not_mistaken_for_a_label(self) -> None:
        # A whole sentence before a URL is not a link name, so the domain stands in.
        text = "Everything I use to film these videos is listed over at https://example.com/gear"
        self.assertEqual(main._description_links(text), [("example.com/gear", "https://example.com/gear")])

    def test_trailing_punctuation_is_not_part_of_the_url(self) -> None:
        self.assertEqual(main._description_links("see (https://example.com).")[0][1], "https://example.com")

    def test_duplicates_appear_once(self) -> None:
        text = "IG: https://example.com\nAlso: https://example.com"
        self.assertEqual(len(main._description_links(text)), 1)

    def test_no_description_means_no_links(self) -> None:
        self.assertEqual(main._description_links(""), [])

    def test_the_label_drops_the_scheme_and_trailing_slash(self) -> None:
        self.assertEqual(main._link_label("https://www.example.com/"), "example.com")


class AboutDialogTests(unittest.TestCase):
    """The full description lives in a dialog, the way YouTube does it."""

    def _dialog(self, description: str = "Welcome to my channel!\n\nIG: https://example.com") -> str:
        return main._channel_about_dialog(
            "Vanessa St. Clair", description, main._description_links(description),
            "UCLjBCYil92x3SYtHrvr3Pew", "vanessa.stclair", "33.3K", 51, "2 days ago", "",
        )

    def test_it_is_a_modal_dialog(self) -> None:
        self.assertIn("<dialog", self._dialog())
        self.assertIn("data-ch-about-dialog", self._dialog())

    def test_it_carries_the_three_youtube_sections(self) -> None:
        markup = self._dialog()
        self.assertIn("<h3>Description</h3>", markup)
        self.assertIn("<h3>Links</h3>", markup)
        self.assertIn("<h3>More info</h3>", markup)

    def test_the_links_section_is_dropped_when_there_are_none(self) -> None:
        markup = self._dialog("Just some words.")
        self.assertNotIn("<h3>Links</h3>", markup)
        self.assertIn("<h3>Description</h3>", markup)

    def test_a_channel_with_nothing_to_say_gets_no_dialog(self) -> None:
        self.assertEqual(self._dialog(""), "")

    def test_it_never_claims_a_join_date_or_a_view_total(self) -> None:
        # This Invidious build reports `joined` and `totalViews` as 0 for every channel,
        # so rendering either would be inventing a number.
        markup = self._dialog()
        self.assertNotIn("Joined", markup)
        self.assertNotIn("views", markup)

    def test_the_video_count_says_whose_count_it_is(self) -> None:
        # 51 is what OpenFeed has stored, not what YouTube reports for the channel.
        self.assertIn("51 videos in your library", self._dialog())

    def test_it_can_be_closed(self) -> None:
        self.assertIn("data-close-dialog", self._dialog())

    def test_outbound_links_do_not_leak_the_referrer(self) -> None:
        self.assertIn('rel="noopener noreferrer nofollow"', self._dialog())

    def test_the_channel_link_prefers_the_handle(self) -> None:
        self.assertIn("youtube.com/@vanessa.stclair", self._dialog())


class AboutInlineTests(unittest.TestCase):
    """Expanding in place moved the avatar, so the inline copy stays one line."""

    def test_the_inline_description_opens_the_dialog_rather_than_expanding(self) -> None:
        source = main._home_script()
        self.assertIn("data-ch-about-open", source)
        self.assertNotIn("data-ch-about-toggle", source)

    def test_the_one_line_clamp_cannot_grow(self) -> None:
        css = main._home_styles()
        self.assertIn(".ch-about-short { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }", css)

    def test_clicking_the_backdrop_closes_it(self) -> None:
        self.assertIn("dialog.ch-about-dialog", main._home_script())


class StylesheetIntegrityTests(unittest.TestCase):
    """The stylesheet is one long Python string, so a broken comment is invisible."""

    def test_no_orphaned_comment_tails(self) -> None:
        # A stray "*/" makes the CSS parser discard everything up to it, which silently
        # dropped the feed-controls dialog sizing rule for as long as it was there. It
        # costs nothing to notice here instead of in a browser.
        css = main._home_styles()
        index = 0
        while True:
            opener = css.find("/*", index)
            closer = css.find("*/", index)
            if opener == -1 and closer == -1:
                break
            if closer != -1 and (opener == -1 or closer < opener):
                self.fail(f"orphaned comment tail near: ...{css[max(0, closer - 70):closer + 2]}")
            end = css.find("*/", opener + 2)
            self.assertNotEqual(end, -1, f"unclosed comment near: {css[opener:opener + 70]}")
            index = end + 2

    def test_closed_dialogs_stay_hidden(self) -> None:
        # `display` on a bare dialog selector beats the browser's own rule for closed
        # dialogs, so the panel renders inline on page load.
        css = main._home_styles()
        for line in css.splitlines():
            if "dialog" in line and "display:" in line and "[open]" not in line:
                self.assertNotIn(".ch-about-dialog {", line)
                self.assertNotIn(".fc-dialog {", line)


class ChannelSearchTests(unittest.TestCase):
    """YouTube's channel search is a magnifier that grows into an underlined field."""

    def test_the_field_is_not_an_outlined_pill(self) -> None:
        css = main._home_styles()
        self.assertNotIn(".ch-search { margin-left: auto; display: flex; align-items: center; gap: 7px;", css)
        self.assertIn(".ch-search input { width: 0;", css)

    def test_it_expands_only_when_opened(self) -> None:
        css = main._home_styles()
        self.assertIn(".ch-search.is-open input { width: min(240px, 34vw); opacity: 1;", css)

    def test_the_text_is_readable_rather_than_tiny(self) -> None:
        css = main._home_styles()
        start = css.index(".ch-search input {")
        self.assertIn("font-size: 14px", css[start:start + 260])

    def test_the_toggle_submits_when_something_is_typed(self) -> None:
        self.assertIn("data-ch-search-toggle", main._home_script())

    def test_it_survives_on_narrow_screens_instead_of_disappearing(self) -> None:
        css = main._home_styles()
        self.assertNotIn(".ch-search { display: none; }", css)


class SubscriptionArtTests(unittest.TestCase):
    """A blank sidebar avatar is the visible symptom of an unenriched channel."""

    def test_subscribed_channels_are_enriched_before_discovered_ones(self) -> None:
        from openfeed import db

        with patch.object(db, "connect") as connect:
            cursor = connect.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
            cursor.fetchall.return_value = []
            db.list_channels_needing_media(12)
            sql = cursor.execute.call_args[0][0]
        self.assertIn("ORDER BY (fs.id IS NOT NULL) DESC", sql)

    def test_the_backfill_walks_only_subscriptions(self) -> None:
        from openfeed import db

        with patch.object(db, "connect") as connect:
            cursor = connect.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
            cursor.fetchall.return_value = []
            db.list_subscription_channels_needing_media(5)
            sql = cursor.execute.call_args[0][0]
        self.assertIn("JOIN channels c ON c.youtube_channel_id = fs.youtube_channel_id", sql)
        self.assertIn("c.avatar_url IS NULL", sql)

    def test_it_keeps_going_until_the_subscriptions_are_done(self) -> None:
        batches = [
            [{"youtube_channel_id": f"UC{index:022d}"} for index in range(enrichment.CHANNEL_MEDIA_BATCH)],
            [{"youtube_channel_id": "UCtail"}],
        ]
        with patch.object(enrichment.db, "list_subscription_channels_needing_media", side_effect=batches), \
             patch.object(enrichment.db, "count_subscription_channels_missing_media", return_value=0), \
             patch.object(enrichment, "enrich_channel", return_value={"ok": True}), \
             patch.object(enrichment, "CHANNEL_MEDIA_GAP_SECONDS", 0):
            result = enrichment.backfill_subscription_art()
        self.assertEqual(result["attempted"], enrichment.CHANNEL_MEDIA_BATCH + 1)
        self.assertEqual(result["remaining"], 0)

    def test_a_channel_invidious_cannot_resolve_does_not_loop_forever(self) -> None:
        # The same unresolvable channel comes back in every batch; a full pass that
        # changes nothing has to stop rather than fetch it until the process dies.
        batch = [{"youtube_channel_id": f"UC{index:022d}"} for index in range(enrichment.CHANNEL_MEDIA_BATCH)]
        with patch.object(enrichment.db, "list_subscription_channels_needing_media", return_value=batch), \
             patch.object(enrichment.db, "count_subscription_channels_missing_media", return_value=len(batch)), \
             patch.object(enrichment, "enrich_channel", return_value={"ok": False}), \
             patch.object(enrichment, "CHANNEL_MEDIA_GAP_SECONDS", 0):
            result = enrichment.backfill_subscription_art()
        self.assertEqual(result["attempted"], enrichment.CHANNEL_MEDIA_BATCH)

    def test_two_passes_do_not_run_at_once(self) -> None:
        enrichment._subscription_art_lock.acquire()
        try:
            result = enrichment.backfill_subscription_art()
        finally:
            enrichment._subscription_art_lock.release()
        self.assertEqual(result["updated"], 0)
        self.assertIn("skipped", result)

    def test_avatars_stay_on_our_own_proxy(self) -> None:
        proxied = enrichment._proxied_ggpht("https://yt3.googleusercontent.com/ytc/AIdro_abc=s176")
        self.assertTrue(proxied.startswith("/ggpht/"))
        self.assertNotIn("googleusercontent.com", proxied)


if __name__ == "__main__":
    unittest.main()


class BrandMarkTests(unittest.TestCase):
    """The logo has to survive being 26px."""

    def test_it_is_a_shape_not_type_set_at_nine_pixels(self) -> None:
        mark = main._brand_mark()
        self.assertIn("/assets/openfeed-logo.png", mark)
        self.assertNotIn(">Open<", mark)

    def test_the_wordmark_carries_the_name(self) -> None:
        header = main._home_styles()
        self.assertIn(".brand-word b { font-weight: 680;", header)

    def test_nothing_in_the_glyph_is_smaller_than_a_stroke(self) -> None:
        # The drawn SVG mark is gone; the served logo file carries its own
        # artwork, so there is no sub-pixel stroke detail left to go mushy.
        mark = main._brand_mark()
        self.assertIn("<img", mark)
        self.assertNotIn("<svg", mark)

    def test_the_tile_still_reads_as_a_button_sized_target(self) -> None:
        css = main._home_styles()
        self.assertIn(".brand-mark { display: grid; place-items: center; width: 26px; height: 26px;", css)


class HeaderSpacingTests(unittest.TestCase):
    """The bell was almost touching the profile circle."""

    def test_the_actions_row_is_no_longer_two_pixels_apart(self) -> None:
        self.assertNotIn(".header-actions { display: flex; justify-content: flex-end; align-items: center; gap: 2px; }", main._home_styles())

    def test_the_avatar_gets_extra_room_of_its_own(self) -> None:
        # The bell is a 34px hit area around a 20px glyph; the avatar is a bare 28px
        # circle, so an equal gap does not look equal.
        self.assertIn(".header-actions .profile-menu { margin-left: 6px; }", main._home_styles())
