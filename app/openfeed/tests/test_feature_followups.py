"""Follow-ups from the integrated QA pass of the feature build."""
import unittest
from unittest.mock import patch

from openfeed import db, main


class MutedCreatorsLeaveEveryHomeShelf(unittest.TestCase):
    def test_catch_up_and_subscription_shelves_drop_muted_creators(self) -> None:
        sections = [
            {"id": "new-subscriptions", "videos": [{"youtube_channel_id": "UCmuted"}, {"youtube_channel_id": "UCkeep"}]},
            {"id": "your-regulars-1", "videos": [{"youtube_channel_id": "UCMUTED"}]},
            {"id": "recommended", "videos": [{"youtube_channel_id": "UCkeep"}]},
        ]
        with patch.object(db, "get_feed_preferences", lambda u: {"muted_channels": ["ucmuted"]}):
            out = main._without_muted_creators(sections, "u")
        self.assertEqual([s["id"] for s in out], ["new-subscriptions", "recommended"], "a shelf left empty is dropped")
        self.assertEqual(out[0]["videos"], [{"youtube_channel_id": "UCkeep"}])

    def test_nothing_muted_changes_nothing(self) -> None:
        sections = [{"id": "x", "videos": [{"youtube_channel_id": "UCa"}]}]
        with patch.object(db, "get_feed_preferences", lambda u: {}):
            self.assertIs(main._without_muted_creators(sections, "u"), sections)


class SearchTypeChipsKeepFilters(unittest.TestCase):
    def test_type_chips_keep_only_the_filters_their_type_uses(self) -> None:
        # Carrying every filter through Channels left sort/date/length/features in the URL
        # with no control showing them. Each chip now keeps what its own type applies.
        from html import escape
        filters = main._search_filters("videos", "view_count", "week", "short", "hd")
        html = main._search_controls("linux", filters)
        self.assertIn(escape(main._search_href("linux", "channels"), quote=True), html)
        self.assertIn(escape(main._search_href("linux", "playlists"), quote=True), html)
        self.assertIn(escape(main._search_href("linux", "shorts", "view_count", "week"), quote=True), html)
        self.assertIn(escape(main._search_href("linux", "all", "view_count", "week", "short", "hd"), quote=True), html)

    def test_clear_filters_stays_while_any_filter_is_in_the_url(self) -> None:
        filters = main._search_filters("channels", "view_count", "week", "short", "hd")
        self.assertEqual((filters["sort"], filters["date"], filters["duration"]), ("relevance", "", ""))
        html = main._search_controls("linux", filters)
        self.assertIn("data-clear-search-filters", html)
        self.assertNotIn("data-clear-search-filters", main._search_controls("linux", main._search_filters("channels", "relevance", "", "", "")))
