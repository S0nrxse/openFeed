"""Autoplay, Share at a time, player defaults in Settings, data export, and API tokens
with sign-in off. DB-free: queries are captured or the db layer is patched."""

from __future__ import annotations

import datetime as dt
import inspect
import json
import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from openfeed import dataexport, db, main, sharetime, tokens
from openfeed.tests.support import offline_db

ASSETS = main._ASSET_DIR


class _FakeRequest:
    def __init__(self, header: str | None = None) -> None:
        self.headers = {"authorization": header} if header else {}
        self.cookies: dict[str, str] = {}


class ShareTimeTests(unittest.TestCase):
    def test_every_form_youtube_writes_is_read(self) -> None:
        for value, seconds in (("90", 90), ("90s", 90), ("1m30s", 90), ("1m30", 90), ("1h2m3s", 3723),
                               ("2m", 120), ("1h", 3600), ("1:30", 90), ("01:02:03", 3723), ("0", 0), (" 45 ", 45)):
            with self.subTest(value=value):
                self.assertEqual(sharetime.parse_start(value), seconds)

    def test_anything_else_is_ignored_rather_than_guessed(self) -> None:
        for value in (None, "", "abc", "-5", "1:75", "1:61:00", "1.5", "5x", "m", "1:2:3:4"):
            with self.subTest(value=value):
                self.assertIsNone(sharetime.parse_start(value))

    def test_a_start_is_capped_at_a_day(self) -> None:
        self.assertEqual(sharetime.parse_start("999999"), sharetime.MAX_START_SECONDS)

    def test_clock_reads_like_the_player(self) -> None:
        self.assertEqual(sharetime.clock(0), "0:00")
        self.assertEqual(sharetime.clock(90), "1:30")
        self.assertEqual(sharetime.clock(3723), "1:02:03")


class WatchStartTests(unittest.TestCase):
    def test_a_linked_time_wins_over_the_resume_point_and_says_nothing(self) -> None:
        self.assertEqual(main._watch_start_point("1m30s", False, 600), (90, False))
        # t=0 is an explicit "from the start", not garbage.
        self.assertEqual(main._watch_start_point("0", False, 600), (0, False))

    def test_without_a_link_time_the_resume_point_stands(self) -> None:
        self.assertEqual(main._watch_start_point(None, False, 600), (600, True))
        self.assertEqual(main._watch_start_point("nonsense", False, 600), (600, True))
        self.assertEqual(main._watch_start_point(None, False, 0), (0, False))

    def test_restart_beats_everything(self) -> None:
        self.assertEqual(main._watch_start_point("90", True, 600), (0, False))

    def test_the_watch_page_uses_it_for_the_player_and_the_note(self) -> None:
        source = inspect.getsource(main.watch_video)
        self.assertIn('_watch_start_point(request.query_params.get("t"), restart, stored_resume)', source)
        self.assertIn("if resuming else", source)
        self.assertIn('src="/watch/{escape(youtube_video_id)}/player{resume_query}"', source)


class PlaybackPreferenceStoreTests(unittest.TestCase):
    def test_defaults_are_autoplay_on_at_normal_speed(self) -> None:
        prefs = db.normalize_playback_preferences({})
        self.assertIs(prefs["autoplay"], True)
        self.assertEqual(prefs["default_speed"], 1.0)

    def test_only_speeds_the_player_offers_are_kept(self) -> None:
        self.assertEqual(db.normalize_playback_preferences({"default_speed": 1.5})["default_speed"], 1.5)
        for bad in (3, 0, -1, "fast", None, 1.3):
            with self.subTest(bad=bad):
                self.assertEqual(db.normalize_playback_preferences({"default_speed": bad})["default_speed"], 1.0)
        self.assertIs(db.normalize_playback_preferences({"autoplay": 0})["autoplay"], False)

    def test_the_api_saves_both_through_the_existing_store(self) -> None:
        client = TestClient(main.app)
        self.addCleanup(client.close)
        with patch.object(db, "connect", side_effect=AssertionError("no database")), \
             patch.object(db, "get_playback_preferences", return_value=dict(db.PLAYBACK_DEFAULTS)), \
             patch.object(db, "set_playback_preferences", return_value=dict(db.PLAYBACK_DEFAULTS)) as save:
            response = client.post("/api/playback/preferences", json={"autoplay": False, "default_speed": 1.25})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(save.call_args.kwargs["autoplay"], False)
        self.assertEqual(save.call_args.kwargs["default_speed"], 1.25)
        # Untouched fields stay None, so one switch never resets another.
        self.assertIsNone(save.call_args.kwargs["captions_enabled"])


class PlayerDefaultsSettingsTests(unittest.TestCase):
    def _html(self, **prefs: object) -> str:
        stored = {**db.PLAYBACK_DEFAULTS, **prefs}
        with patch.object(db, "get_playback_preferences", return_value=stored):
            return main._player_defaults_settings_html("local")

    def test_every_default_has_a_control_bound_to_its_stored_key(self) -> None:
        html = self._html(default_speed=1.5, captions_enabled=True, captions_language="de")
        for key in ("autoplay", "default_speed", "captions_enabled", "captions_language"):
            self.assertIn(f'data-playback-pref="{key}"', html)
        self.assertIn('<option value="1.5" selected>1.5x</option>', html)
        self.assertIn('<option value="1">Normal</option>', html)
        self.assertIn('<option value="de" selected>German</option>', html)
        self.assertIn('name="captions_enabled" checked', html)
        self.assertIn('name="autoplay" checked', html)
        self.assertIn('aria-label="Default playback speed"', html)

    def test_a_language_from_the_player_menu_stays_selectable(self) -> None:
        self.assertIn('<option value="pt-BR" selected>pt-BR</option>', self._html(captions_language="pt-BR"))

    def test_a_database_failure_still_draws_the_defaults(self) -> None:
        with patch.object(db, "get_playback_preferences", side_effect=RuntimeError("down")):
            html = main._player_defaults_settings_html("local")
        self.assertIn('data-playback-pref="autoplay"', html)

    def test_the_playback_pane_opens_with_them_and_live_chat_keeps_its_switch(self) -> None:
        with offline_db():
            html = main._settings_dialog(user={"authenticated": False})
        playback = html.split('data-settings-pane-body="playback"')[1].split('data-settings-pane-body="content"')[0]
        self.assertLess(playback.index('data-playback-pref="autoplay"'), playback.index("data-playback-sources"))
        self.assertIn("Live chat", html.split('data-settings-pane-body="integrations"')[1])

    def test_the_player_shell_gets_the_default_speed_only_when_it_is_not_normal(self) -> None:
        with patch.object(db, "get_playback_preferences", return_value={"default_speed": 1.75}):
            self.assertEqual(main._default_speed_attr("local"), "1.75")
        with patch.object(db, "get_playback_preferences", return_value={"default_speed": 1.0}):
            self.assertEqual(main._default_speed_attr("local"), "")
        with patch.object(db, "get_playback_preferences", side_effect=RuntimeError("down")):
            self.assertEqual(main._default_speed_attr("local"), "")
        self.assertIn('media_meta["default-speed"] = _default_speed_attr(user_key)', inspect.getsource(main.watch_player))


class PlayerKitTests(unittest.TestCase):
    kit = (ASSETS / "player" / "kit" / "media-kit.js").read_text(encoding="utf-8")

    def test_the_bar_switch_is_a_named_switch_that_reports_up(self) -> None:
        self.assertIn("button.setAttribute('role', 'switch');", self.kit)
        self.assertIn("button.setAttribute('aria-label', 'Autoplay');", self.kit)
        self.assertIn("post({ type: 'openfeed:autoplay-set', on: state });", self.kit)
        # Hidden until the page says, so it never shows a guess.
        self.assertIn("button.hidden = state === null;", self.kit)
        self.assertIn("setAutoplay: autoplay.set,", self.kit)

    def test_default_speed_goes_through_video_js_when_present(self) -> None:
        self.assertIn("player.playbackRate(speed)", self.kit)
        self.assertIn("setupSpeed(context, Number(options.speed) || 1);", self.kit)

    def test_always_show_captions_picks_a_track_without_a_remembered_language(self) -> None:
        self.assertIn("|| pick(navigator.language || 'en', false)", self.kit)
        self.assertIn("|| tracks.find((track) => !track.auto) || tracks[0];", self.kit)

    def test_the_shell_forwards_the_state_and_the_speed(self) -> None:
        source = main._player_script()
        self.assertTrue("data.type === 'openfeed:autoplay-state'" in source)
        self.assertTrue("autoplay: autoplayState, speed: Number(stage.dataset.defaultSpeed) || 1," in source)

    def test_the_switch_style_travels_with_the_player_chrome(self) -> None:
        css = main._player_chrome_css()
        self.assertIn(".of-autoplay-control.is-on .of-autoplay-track { background: var(--of-bone); }", css)
        # Spliced into a template literal.
        own = (ASSETS / "player" / "playback-prefs.css").read_text(encoding="utf-8")
        for forbidden in ("`", "\\", "${"):
            self.assertNotIn(forbidden, own)


class AutoplayPageTests(unittest.TestCase):
    script = (ASSETS / "js" / "playback-prefs.js").read_text(encoding="utf-8")

    def test_it_ships_in_the_page_bundle(self) -> None:
        self.assertIn("openfeed:autoplay-state", main._home_script())

    def test_a_list_and_shorts_keep_their_own_behaviour(self) -> None:
        self.assertIn("listHasNext()", self.script)
        self.assertIn("document.querySelector('[data-watch-list] [data-list-next] a[href^=\"/watch/\"]')", self.script)
        self.assertIn("if (seconds > 0 && seconds <= 90) continue;", self.script)
        self.assertIn("const isShort = Boolean(document.querySelector('.watch-player-shell.is-short'));", self.script)

    def test_the_countdown_has_cancel_and_play_now(self) -> None:
        for text in ("'Cancel'", "'Play now'", "Up next in ", "const COUNTDOWN_SECONDS = 8;"):
            self.assertIn(text, self.script)

    def test_only_after_it_has_played(self) -> None:
        self.assertIn("if (!played || !autoplayOn || cancelledAtEnd || leaving || listHasNext()) return;", self.script)


class ShareDialogTests(unittest.TestCase):
    def _dialog(self, autoplay: bool = True, short: bool = False) -> str:
        with patch.object(db, "get_playback_preferences", return_value={"autoplay": autoplay}):
            return main._watch_share_dialog("dQw4w9WgXcQ", "local", short)

    def test_the_dialog_has_the_link_copy_and_start_at(self) -> None:
        html = self._dialog()
        self.assertIn('aria-labelledby="share-dialog-title"', html)
        self.assertIn('value="/watch/dQw4w9WgXcQ"', html)
        self.assertIn("data-share-copy", html)
        self.assertIn('<span>Start at</span>', html)
        self.assertIn('aria-label="Start time, minutes and seconds" disabled', html)
        self.assertIn('value="youtube"', html)

    def test_it_carries_the_autoplay_state_and_shorts_never_autoplay(self) -> None:
        self.assertIn('data-autoplay="1"', self._dialog(True))
        self.assertIn('data-autoplay="0"', self._dialog(False))
        self.assertIn('data-autoplay="0"', self._dialog(True, short=True))
        with patch.object(db, "get_playback_preferences", side_effect=RuntimeError("down")):
            self.assertIn('data-autoplay="0"', main._watch_share_dialog("x", "local"))

    def test_the_watch_page_opens_it_from_share(self) -> None:
        source = inspect.getsource(main.watch_video)
        self.assertIn("data-share-open aria-haspopup=\"dialog\"", source)
        self.assertIn("_watch_share_dialog(youtube_video_id, user_key, bool(short_class))", source)

    def test_the_link_is_written_the_way_watch_reads_it(self) -> None:
        script = (ASSETS / "js" / "playback-prefs.js").read_text(encoding="utf-8")
        self.assertIn("url.searchParams.set('t', String(seconds));", script)
        self.assertIn("url.searchParams.set('t', `${seconds}s`);", script)

    def test_its_styles_are_in_the_bundle(self) -> None:
        css = main._home_styles()
        self.assertIn(".share-dialog[open] { width: min(480px, calc(100vw - 32px)); }", css)
        self.assertIn(".autoplay-card {", css)


def _fake_run(rows: dict[str, list[dict]]):
    calls: list[tuple[str, dict]] = []

    def run(sql: str, params: dict) -> list[dict]:
        calls.append((sql, params))
        name = next(key for key, query in dataexport._QUERIES.items() if query == sql)
        return rows.get(name, [])
    return run, calls


class DataExportTests(unittest.TestCase):
    NOW = dt.datetime(2026, 11, 9, 12, 0, tzinfo=dt.timezone.utc)

    def test_every_query_including_subscriptions_is_scoped_to_the_user(self) -> None:
        for name, sql in dataexport._QUERIES.items():
            with self.subTest(name=name):
                self.assertIn("%(user)s", sql)
        self.assertIn("JOIN user_subscriptions us", dataexport._QUERIES["subscriptions"])
        run, calls = _fake_run({})
        data = dataexport.build_export("alice:p:1", run=run, now=self.NOW)
        self.assertEqual(len(calls), len(dataexport._QUERIES))
        for sql, params in calls:
            self.assertEqual(params, {"user": "alice:p:1"})
            self.assertNotIn("alice", sql)
        self.assertFalse(any("whole install" in note for note in data["notes"]))

    def test_the_opml_download_is_the_callers_list(self) -> None:
        captured = []
        with patch.object(dataexport, "_run_with_db", side_effect=lambda sql, params: captured.append(params) or []):
            dataexport.subscription_channels("bob")
        self.assertEqual(captured, [{"user": "bob"}])

    def test_sections_are_folded_from_their_rows(self) -> None:
        added = dt.datetime(2026, 1, 2, tzinfo=dt.timezone.utc)
        run, _ = _fake_run({
            "subscriptions": [{"id": "s1", "title": "Chan", "feed_url": "https://www.youtube.com/feeds/videos.xml?channel_id=UC1",
                               "html_url": "https://www.youtube.com/channel/UC1", "youtube_channel_id": "UC1", "subscribed_at": added}],
            "collections": [
                {"id": "c1", "name": "Tech", "subscription_id": "s1", "subscription_title": "Chan", "added_at": added},
                {"id": "c2", "name": "Empty", "subscription_id": None},
            ],
            "playlists": [
                {"id": "p1", "name": "Mix", "youtube_video_id": "v1", "video_title": "One", "position": 0},
                {"id": "p1", "name": "Mix", "youtube_video_id": "v2", "video_title": "Two", "position": 1},
                {"id": "p2", "name": "Watch later", "youtube_video_id": "v3", "video_title": "Three", "position": 0},
            ],
            "liked_videos": [{"youtube_video_id": "v4", "video_title": "Four", "liked_at": added}],
            "watch_history": [{"youtube_video_id": "v5", "percent_completed": 50, "resume_seconds": 61.5, "completed": False, "open_count": 2},
                              {"youtube_video_id": "v6", "percent_completed": 0.25}],
            "video_feedback": [{"youtube_video_id": "v7", "not_interested": True, "not_interested_reason": "Seen it"}],
            "feedback_requests": [{"youtube_video_id": "v8", "event_type": "less_like_this", "created_at": added}],
            "settings": [{"profile": {"muted_channels": ["uc9"], "hidden_shelves": ["catchup"], "playback": {"autoplay": False}},
                          "topics": [{"topic": "chess", "preference": "follow"}]}],
            "notifications": [{"youtube_channel_id": "UC1", "level": "none"}],
        })
        data = dataexport.build_export("local", profile_name="Main", run=run, now=self.NOW)
        self.assertEqual((data["format"], data["version"]), ("openfeed-export", 1))
        self.assertEqual(data["subscriptions"]["channels"][0]["subscribed_at"], "2026-01-02T00:00:00+00:00")
        self.assertEqual([c["name"] for c in data["subscriptions"]["collections"]], ["Tech", "Empty"])
        self.assertEqual(data["subscriptions"]["collections"][1]["subscriptions"], [])
        self.assertEqual([p["name"] for p in data["playlists"]], ["Mix"])
        self.assertEqual([i["youtube_video_id"] for i in data["playlists"][0]["items"]], ["v1", "v2"])
        self.assertEqual([i["youtube_video_id"] for i in data["watch_later"]], ["v3"])
        self.assertEqual(data["liked_videos"][0]["url"], "https://www.youtube.com/watch?v=v4")
        self.assertEqual([h["fraction_watched"] for h in data["watch_history"]], [0.5, 0.25])
        self.assertEqual(data["watch_history"][0]["resume_seconds"], 61.5)
        self.assertEqual(data["feedback"]["videos"][0]["reason"], "Seen it")
        self.assertEqual(data["feedback"]["more_or_less_like_this"][0]["request"], "less")
        self.assertEqual(data["feedback"]["muted_channels"], ["uc9"])
        self.assertIs(data["settings"]["playback"]["autoplay"], False)
        self.assertEqual(data["settings"]["topics"], [{"topic": "chess", "preference": "follow"}])
        self.assertNotIn("playback", data["settings"]["feed"])
        json.loads(dataexport.to_json(data))

    def test_opml_is_escaped_and_skips_feedless_rows(self) -> None:
        opml = dataexport.subscriptions_opml([
            {"title": 'A & "B" <c>', "feed_url": "https://x/feed?a=1&b=2", "channel_url": "https://x"},
            {"title": "No feed", "feed_url": ""},
        ], now=self.NOW)
        self.assertIn('text="A &amp; &quot;B&quot; &lt;c&gt;"', opml)
        self.assertIn('xmlUrl="https://x/feed?a=1&amp;b=2"', opml)
        self.assertNotIn("No feed", opml)
        import xml.etree.ElementTree as ET
        self.assertEqual(len(ET.fromstring(opml).findall(".//outline[@type='rss']")), 1)

    def test_the_routes_download_files(self) -> None:
        client = TestClient(main.app)
        self.addCleanup(client.close)
        with patch.object(db, "connect", side_effect=AssertionError("no database")), \
             patch.object(dataexport, "build_export", return_value={"format": "openfeed-export"}) as build, \
             patch.object(dataexport, "subscription_channels", return_value=[]):
            response = client.get("/api/export")
            opml = client.get("/api/export/subscriptions.opml")
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment; filename=\"openfeed-export-", response.headers["content-disposition"])
        self.assertEqual(response.json(), {"format": "openfeed-export"})
        self.assertEqual(build.call_args.args[0], "local")
        self.assertEqual(opml.status_code, 200)
        self.assertIn(".opml", opml.headers["content-disposition"])
        self.assertIn("<opml", opml.text)

    def test_settings_offers_both(self) -> None:
        with offline_db():
            html = main._settings_dialog(user={"authenticated": False})
        account = html.split('data-settings-pane-body="account"')[1]
        self.assertIn('href="/api/export" download', account)
        self.assertIn('href="/api/export/subscriptions.opml" download', account)
        self.assertIn("Export your data", account)


class LocalTokenTests(unittest.TestCase):
    """Sign-in off: the local profile can hold tokens, and they die when sign-in comes on."""

    def setUp(self) -> None:
        self.enterContext(patch.dict(os.environ, {"OPENFEED_AUTH_MODE": "disabled", "OPENFEED_REQUIRE_LOGIN": "false"}, clear=True))
        self.enterContext(patch.object(db, "connect", side_effect=AssertionError("Unexpected database access")))
        self.plaintext, prefix, digest = tokens.generate()
        self.record = {"id": "00000000-0000-0000-0000-000000000002", "user_key": "local", "name": "script",
                       "token_prefix": prefix, "token_hash": digest, "read_only": True, "revoked_at": None,
                       "delegated_admin": True}
        self.enterContext(patch.object(db, "find_api_token", return_value=self.record))
        self.enterContext(patch.object(db, "touch_api_token"))
        self.client = TestClient(main.app)
        self.addCleanup(self.client.close)

    def test_the_local_browser_can_list_and_create_tokens(self) -> None:
        with patch.object(db, "list_api_tokens", return_value=[]):
            self.assertEqual(self.client.get("/api/tokens").status_code, 200)
        with patch.object(db, "create_api_token", return_value=self.record) as create:
            response = self.client.post("/api/tokens", json={"name": "home script", "read_only": True})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(create.call_args.args[4], "local")
        self.assertTrue(response.json()["token"].startswith("ofd_"))

    def test_a_local_token_works_while_sign_in_is_off(self) -> None:
        user = main._user_from_token(_FakeRequest(f"Bearer {self.plaintext}"))
        self.assertEqual(user["user_key"], "local")
        self.assertTrue(user["token_read_only"])
        # Read only is still enforced on calls that carry the token.
        self.client.headers["authorization"] = f"Bearer {self.plaintext}"
        self.assertEqual(self.client.post("/api/playback/preferences", json={}).status_code, 403)

    def test_tokens_still_cannot_manage_tokens(self) -> None:
        self.client.headers["authorization"] = f"Bearer {self.plaintext}"
        self.assertEqual(self.client.get("/api/tokens").status_code, 403)

    def test_local_tokens_stop_working_once_sign_in_is_on_or_required(self) -> None:
        for env in ({"OPENFEED_AUTH_MODE": "authentik_oidc"}, {"OPENFEED_REQUIRE_LOGIN": "true"}):
            for owner in ("local", "local:p:0123abcd"):
                with self.subTest(env=env, owner=owner), patch.dict(os.environ, env), \
                     patch.dict(self.record, {"user_key": owner}):
                    self.assertIsNone(main._user_from_token(_FakeRequest(f"Bearer {self.plaintext}")))
                    self.client.headers["authorization"] = f"Bearer {self.plaintext}"
                    self.assertEqual(self.client.get("/api/playback/preferences").status_code, 401)

    def test_an_account_token_is_untouched_by_the_rule(self) -> None:
        from openfeed import accounts

        # An active account, or an Authentik user from before accounts with no row yet.
        for state in ("active", "unknown"):
            with self.subTest(state=state), patch.dict(os.environ, {"OPENFEED_AUTH_MODE": "authentik_oidc"}), \
                 patch.dict(self.record, {"user_key": "alice"}), patch.object(accounts, "owner_state", return_value=state):
                self.assertEqual(main._user_from_token(_FakeRequest(f"Bearer {self.plaintext}"))["user_key"], "alice")

    def test_the_pane_offers_the_form_and_says_what_a_token_means_here(self) -> None:
        html = main._api_tokens_pane({"authenticated": False})
        self.assertIn("data-api-token-form", html)
        self.assertIn("data-api-token-open-note", html)
        self.assertIn("also answers calls that send no token", html)
        signed_in = main._api_tokens_pane({"authenticated": True})
        self.assertNotIn("data-api-token-open-note", signed_in)


if __name__ == "__main__":
    unittest.main()
