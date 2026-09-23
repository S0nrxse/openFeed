from __future__ import annotations

import unittest
from unittest.mock import patch

from openfeed import db, main


def _row(index: int) -> dict[str, object]:
    return {
        "title": f"Channel {index}",
        "youtube_channel_id": f"UCtestchannel{index:012d}",
        "channel_avatar_url": None,
        "avatar_url": None,
        "latest_video_at": None,
    }


class SidebarSubscriptionGroupTests(unittest.TestCase):
    def test_probe_renders_visible_plus_one_with_rest_hidden(self) -> None:
        with patch.object(db, "list_subscriptions", return_value=[_row(i) for i in range(20)]):
            html = main._sidebar_subscription_group()

        self.assertEqual(html.count('class="nav-item nav-channel"'), 7)
        self.assertEqual(html.count('nav-channel-extra'), 1)
        self.assertEqual(html.count(' hidden>'), 1)
        self.assertIn("Show more", html)

    def test_full_list_counts_remaining_exactly(self) -> None:
        with patch.object(db, "list_subscriptions", return_value=[_row(i) for i in range(10)]):
            html = main._sidebar_subscription_group(limit=250)

        self.assertEqual(html.count('class="nav-item nav-channel"'), 7)
        self.assertEqual(html.count('nav-channel-extra'), 3)
        self.assertEqual(html.count(' hidden>'), 3)
        self.assertIn("Show 3 more", html)

    def test_no_toggle_when_everything_fits(self) -> None:
        with patch.object(db, "list_subscriptions", return_value=[_row(i) for i in range(5)]):
            html = main._sidebar_subscription_group()

        self.assertNotIn("data-subscription-more", html)
        self.assertNotIn(" hidden", html)
