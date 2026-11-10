"""The settings, feed controls, tokens, Takeout and first-run surfaces."""

from __future__ import annotations

import inspect
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from openfeed import main
from openfeed.tests.support import offline_db


def _settings_html(user: dict | None = None) -> str:
    with offline_db():
        return main._settings_dialog(user=user or {"authenticated": False})


class SettingsDialogTests(unittest.TestCase):
    def test_every_pane_has_a_tab_and_keeps_its_deep_link_key(self) -> None:
        html = _settings_html()
        for key in ("playback", "content", "integrations", "import", "api-tokens", "account", "services", "status"):
            self.assertIn(f'data-settings-pane="{key}"', html)
            self.assertIn(f'data-settings-pane-body="{key}"', html)

    def test_the_rail_has_no_gradient(self) -> None:
        self.assertNotIn("linear-gradient", main._home_styles().split(".settings-nav {")[1].split("}")[0])

    def test_phone_gets_a_back_control_and_the_view_attribute(self) -> None:
        html = _settings_html()
        self.assertIn("data-settings-back", html)
        self.assertIn('data-settings-view="list"', html)

    def test_settings_rows_use_the_shared_controls(self) -> None:
        # Five switches, Help tune recommendations, DeArrow's master and thumbnails switches,
        # Playback's Autoplay and Always show captions, and Privacy's mirrors of SponsorBlock,
        # DeArrow and RYD (the YouTube and public-instance players are off on this server).
        # Live chat adds a fourteenth only where yt-dlp is installed, as it is in the image.
        with patch.object(main.ytdlp, "available", return_value=False):
            html = _settings_html()
        self.assertEqual(html.count('class="switch"'), 13)
        with patch.object(main.ytdlp, "available", return_value=True):
            self.assertEqual(_settings_html().count('class="switch"'), 14)
        for hook in ("data-setting-shorts", "data-setting-comments", "data-setting-posts", "data-setting-ryd", "data-setting-sponsorblock", "data-setting-explore", "data-setting-dearrow", "data-setting-dearrow-thumbnails"):
            self.assertIn(hook, html)

    def test_status_opens_on_a_skeleton_not_text(self) -> None:
        html = _settings_html()
        self.assertNotIn("Loading status&hellip;", html)
        self.assertIn("status-view is-loading", html)

    def test_saves_report_a_named_failure(self) -> None:
        script = main._home_script()
        self.assertIn("function failureCause", script)
        self.assertIn("'Not saved'", script)

    def test_the_sign_in_action_is_absent_when_sign_in_is_off(self) -> None:
        with patch.dict("os.environ", {"OPENFEED_AUTH_MODE": "disabled"}):
            self.assertNotIn("Sign in with Authentik", _settings_html().split('data-settings-pane-body="account"')[1])


class ApiTokensPaneTests(unittest.TestCase):
    def _settings(self, **overrides):
        values = dict(auth_mode="authentik_oidc", auth_configured=True, require_login=False)
        values.update(overrides)
        return SimpleNamespace(**values)

    def _pane(self, user: dict, **overrides) -> str:
        with patch.object(main, "get_settings", return_value=self._settings(**overrides)):
            return main._api_tokens_pane(user)

    def test_sign_in_off_but_required_says_why_and_offers_no_dead_link(self) -> None:
        html = self._pane({"authenticated": False}, auth_mode="disabled", require_login=True)
        self.assertIn("unavailable because sign-in is off", html)
        self.assertNotIn("data-api-token-form", html)
        self.assertIn("OPENFEED_AUTH_MODE=authentik_oidc", html)
        self.assertNotIn("/auth/login", html)

    def test_unconfigured_sign_in_lists_what_is_missing(self) -> None:
        with patch.object(main, "_auth_missing_envs", return_value=["OPENFEED_OIDC_ISSUER"]):
            html = self._pane({"authenticated": False}, auth_configured=False)
        self.assertIn("OPENFEED_OIDC_ISSUER", html)
        self.assertNotIn("/auth/login", html)

    def test_working_sign_in_links_to_it(self) -> None:
        self.assertIn("/auth/login", self._pane({"authenticated": False}))

    def test_a_token_cannot_manage_tokens(self) -> None:
        html = self._pane({"authenticated": True, "via_token": True})
        self.assertNotIn("data-api-token-form", html)

    def test_a_session_gets_the_form_and_the_reveal_panel(self) -> None:
        html = self._pane({"authenticated": True})
        for hook in ("data-api-token-form", "data-api-token-result", "data-api-token-copy", "data-api-token-dismiss", "data-api-token-list"):
            self.assertIn(hook, html)

    def test_revoke_goes_through_the_shared_confirm_and_names_the_token(self) -> None:
        script = (Path(main.__file__).parent / "assets" / "api-tokens-v1.js").read_text(encoding="utf-8")
        self.assertIn("window.ofConfirm", script)
        self.assertIn("`Revoke ${label}?`", script)
        self.assertNotIn("window.confirm", script)

    def test_the_secret_is_still_cleared_on_close_and_pagehide(self) -> None:
        script = (Path(main.__file__).parent / "assets" / "api-tokens-v1.js").read_text(encoding="utf-8")
        self.assertIn("dialog.addEventListener('close'", script)
        self.assertIn("window.addEventListener('pagehide'", script)
        self.assertIn("redirect: 'error'", script)


class FeedControlsTests(unittest.TestCase):
    def test_muted_creators_do_not_use_ember(self) -> None:
        css = (Path(main.__file__).parent / "assets" / "css" / "65-feed-controls.css").read_text(encoding="utf-8")
        for rule in css.split("}"):
            if ".is-muted" in rule:
                self.assertNotIn("--accent", rule)

    def test_the_dialog_invents_no_numbers(self) -> None:
        with offline_db():
            html = main._feed_dialog("local")
        self.assertNotIn("fc-bar-fill", html)
        self.assertIn("skeleton", html)

    def test_every_ranker_signal_has_a_label(self) -> None:
        import re

        ranker = (Path(main.__file__).parent / "db.py").read_text(encoding="utf-8")
        keys = set(re.findall(r'debug\["([a-z_]+)"\]', ranker))
        self.assertGreater(len(keys), 10)
        script = main._home_script()
        for key in keys:
            self.assertIn(f"{key}: [", script)


class TakeoutStageTests(unittest.TestCase):
    def test_no_stage_claims_a_review_the_server_does_not_have(self) -> None:
        html = main._takeout_stages(1)
        self.assertNotIn("Review", html)
        for label in ("Choose", "Upload and read", "Imported"):
            self.assertIn(label, html)

    def test_stage_markers(self) -> None:
        self.assertEqual(main._takeout_stages(1).count("is-current"), 1)
        self.assertEqual(main._takeout_stages(2).count("is-done"), 1)
        self.assertEqual(main._takeout_stages(3).count("is-done"), 3)

    def test_undo_needs_a_typed_confirmation(self) -> None:
        self.assertIn("typed: 'delete import'", main._home_script())


class PlaybackTestRouteTests(unittest.TestCase):
    def test_one_source_can_be_probed_so_progress_is_real(self) -> None:
        self.assertIn("source_id", inspect.signature(main.api_playback_test).parameters)
        self.assertIn("/api/playback/sources/test?source_id=", main._home_script())

    def test_the_whole_chain_is_reported_so_skipped_hops_can_be_shown(self) -> None:
        self.assertEqual(len(main.PLAYBACK_HOPS), 4)


class SetupChecklistTests(unittest.TestCase):
    def _render(self, *, preferences=None, subscriptions=0, history=0, imported=None, played=False) -> str:
        sources = [{"id": "invidious"}]
        health = {"last_ok": 3 if played else None}
        with patch.object(main.db, "get_feed_preferences", return_value=preferences or {}), \
                patch.object(main.db, "list_subscriptions", return_value=[{}] * subscriptions), \
                patch.object(main.db, "list_history_videos", return_value=[{}] * history), \
                patch.object(main.db, "latest_takeout_import", return_value=imported), \
                patch.object(main, "_configured_playback_sources", return_value=sources), \
                patch.object(main, "source_health_snapshot", return_value=health):
            return main._setup_checklist("local")

    def test_an_empty_install_shows_all_four_rows_undone(self) -> None:
        html = self._render()
        for title in ("Import Takeout", "Add a subscription", "Check playback health", "Optional integrations"):
            self.assertIn(title, html)
        self.assertIn("0 of 4", html)

    def test_rows_complete_from_real_reads(self) -> None:
        html = self._render(subscriptions=3, played=True, preferences={"ryd_enabled": True})
        self.assertIn("3 of 4", html)
        self.assertIn("3 channels followed", html)

    def test_an_established_install_never_sees_it(self) -> None:
        self.assertEqual(self._render(subscriptions=3, history=1), "")
        self.assertEqual(self._render(subscriptions=3, imported={"summary": {}}), "")

    def test_dismissal_sticks(self) -> None:
        self.assertEqual(self._render(preferences={"setup_dismissed": True}), "")

    def test_a_failed_read_leaves_home_alone(self) -> None:
        with patch.object(main.db, "get_feed_preferences", side_effect=RuntimeError("down")):
            self.assertEqual(main._setup_checklist("local"), "")


if __name__ == "__main__":
    unittest.main()
