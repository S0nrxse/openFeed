"""Captions, Media Session, casting and chat on the watch page. No database, no network."""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from openfeed import captions, db, livechat, main, ytdlp

APP = Path(main.__file__).parent
KIT = (APP / "assets" / "player" / "kit" / "media-kit.js").read_text(encoding="utf-8")
WATCH_JS = (APP / "assets" / "js" / "watch-media.js").read_text(encoding="utf-8")
MEDIA_CSS = (APP / "assets" / "player" / "media.css").read_text(encoding="utf-8")

# YouTube's automatic captions, as Invidious companion serves them: each line is typed out
# with word timings, then repeated in a 10 ms cue that scrolls it up.
# The lines holding a single space are real: YouTube puts them inside cues. They are spelled
# out as SPACE so no editor can strip them.
SPACE = " "
KARAOKE = "\n".join([
    "WEBVTT", "Kind: captions", "Language: en", "",
    "00:00:00.000 --> 00:00:03.030 align:start position:0%", SPACE,
    "On<00:00:00.280><c> June</c><00:00:00.480><c> 26th,</c>", "",
    "00:00:03.030 --> 00:00:03.040 align:start position:0%", "On June 26th,", SPACE, "",
    "00:00:03.040 --> 00:00:05.590 align:start position:0%", "On June 26th,",
    "Mexico,<00:00:03.760><c> officers</c><00:00:04.320><c> were</c>", "",
    "00:00:05.590 --> 00:00:05.600 align:start position:0%", "Mexico, officers were", SPACE, "",
])

MANUAL = """WEBVTT
Kind: captions
Language: en

STYLE
::cue { color: red }

00:00:18.640 --> 00:00:21.880
<c.colorE5E5E5>We're no</c> <i>strangers</i>

00:01:02.000 --> 00:01:04.500 line:10%
second &amp; line
"""


class CleanVttTests(unittest.TestCase):
    def test_automatic_captions_show_each_line_once(self) -> None:
        cues = captions.parse_cues(captions.clean_vtt(KARAOKE, auto=True))
        self.assertEqual([lines for _, _, _, lines in cues], [["On June 26th,"], ["Mexico, officers were"]])

    def test_word_timings_and_class_tags_are_removed(self) -> None:
        text = captions.clean_vtt(KARAOKE, auto=True)
        self.assertNotIn("<c>", text)
        self.assertNotIn("<00:", text)
        self.assertTrue(text.startswith("WEBVTT\n\n"))

    def test_automatic_captions_lose_their_left_alignment(self) -> None:
        self.assertNotIn("position:0%", captions.clean_vtt(KARAOKE, auto=True))

    def test_a_line_holding_one_space_does_not_split_a_cue(self) -> None:
        start, end, _, lines = captions.parse_cues(KARAOKE)[0]
        self.assertEqual((start, end), (0.0, 3.03))
        self.assertEqual(lines, ["On June 26th,"])

    def test_manual_captions_keep_italics_settings_and_entities_but_not_styles(self) -> None:
        text = captions.clean_vtt(MANUAL)
        self.assertIn("We're no <i>strangers</i>", text)
        self.assertIn("00:01:02.000 --> 00:01:04.500 line:10%", text)
        self.assertIn("second &amp; line", text)
        self.assertNotIn("STYLE", text)
        self.assertNotIn("colorE5E5E5", text)

    def test_hour_long_timestamps_survive(self) -> None:
        text = captions.clean_vtt("WEBVTT\n\n01:02:03.004 --> 01:02:05.000\nlate\n")
        self.assertIn("01:02:03.004 --> 01:02:05.000", text)

    def test_no_cues_gives_an_empty_but_valid_file(self) -> None:
        self.assertEqual(captions.clean_vtt("WEBVTT\n\n"), "WEBVTT\n\n")


class TrackListingTests(unittest.TestCase):
    def setUp(self) -> None:
        captions.clear_cache()

    def tearDown(self) -> None:
        captions.clear_cache()

    def test_embed_tracks_are_read_with_entities_decoded(self) -> None:
        page = ('<video><track kind="captions" src="/companion/api/v1/captions/abc?label=English (auto-generated)&amp;check=a+b=" '
                'label="English (auto-generated)"><track kind="metadata" src="/x" label="Chapters"></video>')
        self.assertEqual(captions.embed_track_sources(page), {"English (auto-generated)": "/companion/api/v1/captions/abc?label=English (auto-generated)&check=a+b="})

    def test_the_signed_url_is_encoded_like_a_browser_would(self) -> None:
        # Spaces are escaped; the check value's + and = must arrive untouched.
        self.assertEqual(captions._browser_encode("/c?label=English (auto)&check=a+b="), "/c?label=English%20(auto)&check=a+b=")

    def test_auto_generated_is_a_flag_not_part_of_the_name(self) -> None:
        self.assertEqual(captions.split_label("English (auto-generated)"), ("English", True))
        self.assertEqual(captions.split_label("German (Germany)"), ("German (Germany)", False))

    def test_ytdlp_offers_manual_tracks_and_only_the_original_auto_track(self) -> None:
        info = {
            "subtitles": {
                "en": [{"ext": "json3", "url": "https://www.youtube.com/api/timedtext?fmt=json3"}, {"ext": "vtt", "url": "https://www.youtube.com/api/timedtext?fmt=vtt", "name": "English"}],
                "live_chat": [{"ext": "json", "url": "https://www.youtube.com/watch?v=x"}],
                "xx": [{"ext": "vtt", "url": "http://10.0.0.1/evil.vtt", "name": "Internal"}],
            },
            "automatic_captions": {
                "en-orig": [{"ext": "vtt", "url": "https://www.youtube.com/api/timedtext?asr=1", "name": "English (Original)"}],
                "fr": [{"ext": "vtt", "url": "https://www.youtube.com/api/timedtext?tlang=fr", "name": "French"}],
            },
        }
        tracks = captions.ytdlp_tracks_from_info(info)
        self.assertEqual([(t["label"], t["language"], t["auto"]) for t in tracks], [("English", "en", False), ("English", "en", True)])

    def test_without_an_orig_track_the_video_language_is_used(self) -> None:
        info = {"language": "de", "automatic_captions": {"de": [{"ext": "vtt", "url": "https://www.youtube.com/t", "name": "German"}], "fr": [{"ext": "vtt", "url": "https://www.youtube.com/f"}]}}
        self.assertEqual([t["language"] for t in captions.ytdlp_tracks_from_info(info)], ["de"])

    def test_ids_are_stable_and_unique(self) -> None:
        tracks = [{"language": "en", "auto": False}, {"language": "en", "auto": True}, {"language": "en", "auto": False}, {"language": "", "auto": False}]
        captions._assign_ids(tracks)
        self.assertEqual([t["id"] for t in tracks], ["en", "en-auto", "en-2", "und"])

    def test_the_public_list_points_at_this_origin_and_hides_upstream(self) -> None:
        upstream = [{"label": "English", "language": "en", "auto": True, "source": "invidious", "upstream": ["http://inv/companion/x"]},
                    {"label": "English", "language": "en", "auto": False, "source": "invidious", "upstream": ["http://inv/y"]}]
        with patch.object(captions, "_invidious_tracks", return_value=upstream), patch.object(captions, "get_settings") as settings:
            settings.return_value.invidious_base_url = "http://inv"
            tracks = captions.list_tracks("abcdefghijk")
        self.assertEqual([t["id"] for t in tracks], ["en", "en-auto"])  # manual first
        self.assertEqual(tracks[0]["src"], "/api/captions/abcdefghijk/en.vtt")
        self.assertNotIn("upstream", json.dumps(tracks))

    def test_ytdlp_is_the_fallback_when_invidious_lists_nothing(self) -> None:
        info = {"subtitles": {"en": [{"ext": "vtt", "url": "https://www.youtube.com/api/timedtext", "name": "English"}]}}
        with patch.object(captions, "_invidious_tracks", return_value=[]), patch.object(captions, "get_settings") as settings, \
                patch.object(ytdlp, "available", return_value=True), patch.object(ytdlp, "probe", return_value=info):
            settings.return_value.invidious_base_url = "http://inv"
            tracks = captions.list_tracks("abcdefghijk")
        self.assertEqual([(t["id"], t["source"]) for t in tracks], [("en", "ytdlp")])

    def test_fetch_falls_through_to_the_next_upstream_and_caches(self) -> None:
        internal = [{"id": "en", "label": "English", "language": "en", "auto": False, "source": "invidious", "upstream": ["http://inv/signed", "http://inv/direct"]}]
        calls = []

        def fetch(url: str, source: str) -> str:
            calls.append(url)
            if url.endswith("signed"):
                return "<html>Sorry</html>"
            return MANUAL

        with patch.object(captions, "_internal_tracks", return_value=internal), patch.object(captions, "_fetch", side_effect=fetch):
            first = captions.fetch_vtt("abcdefghijk", "en")
            second = captions.fetch_vtt("abcdefghijk", "en")
        self.assertEqual(calls, ["http://inv/signed", "http://inv/direct"])
        self.assertEqual(first, second)
        self.assertIn("strangers", first)

    def test_unknown_tracks_and_dead_sources_are_told_apart(self) -> None:
        internal = [{"id": "en", "label": "English", "language": "en", "auto": False, "source": "invidious", "upstream": ["http://inv/x"]}]
        with patch.object(captions, "_internal_tracks", return_value=internal), patch.object(captions, "_fetch", return_value="WEBVTT\n\n"):
            with self.assertRaises(LookupError):
                captions.fetch_vtt("abcdefghijk", "fr")
            with self.assertRaises(captions.CaptionError):
                captions.fetch_vtt("abcdefghijk", "en")

    def test_ytdlp_caption_urls_cannot_reach_an_internal_address(self) -> None:
        with self.assertRaises(Exception) as caught:
            captions._fetch("http://127.0.0.1:5432/x", "ytdlp")
        self.assertIsInstance(caught.exception, captions.netguard.UnsafeExternalUrl)

    def test_a_remembered_choice_finds_its_track(self) -> None:
        tracks = [{"id": "en", "language": "en", "auto": False}, {"id": "en-auto", "language": "en", "auto": True}, {"id": "pt-BR", "language": "pt-BR", "auto": False}]
        self.assertEqual(captions.pick_track(tracks, "en", True)["id"], "en-auto")
        self.assertEqual(captions.pick_track(tracks, "en-GB", False)["id"], "en")
        self.assertEqual(captions.pick_track(tracks, "pt", False)["id"], "pt-BR")
        self.assertIsNone(captions.pick_track(tracks, "ja", False))
        self.assertIsNone(captions.pick_track(tracks, "", False))


class CaptionApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(main.app)

    def test_the_list_carries_the_remembered_choice(self) -> None:
        tracks = [{"id": "en", "label": "English", "language": "en", "auto": False, "source": "invidious", "src": "/api/captions/abcdefghijk/en.vtt"}]
        prefs = db.normalize_playback_preferences({"captions_enabled": True, "captions_language": "en", "captions_auto": False})
        with patch.object(captions, "list_tracks", return_value=tracks), patch.object(db, "get_playback_preferences", return_value=prefs):
            body = self.client.get("/api/captions/abcdefghijk").json()
        self.assertEqual(body["tracks"], tracks)
        self.assertEqual(body["preference"], {"enabled": True, "language": "en", "auto": False})

    def test_a_track_is_served_as_webvtt(self) -> None:
        with patch.object(captions, "fetch_vtt", return_value="WEBVTT\n\n"):
            response = self.client.get("/api/captions/abcdefghijk/en-auto.vtt")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/vtt"))

    def test_failures_have_their_own_status(self) -> None:
        with patch.object(captions, "fetch_vtt", side_effect=LookupError("x")):
            self.assertEqual(self.client.get("/api/captions/abcdefghijk/xx.vtt").status_code, 404)
        with patch.object(captions, "fetch_vtt", side_effect=captions.CaptionError("blocked")):
            self.assertEqual(self.client.get("/api/captions/abcdefghijk/en.vtt").status_code, 502)

    def test_a_malformed_id_never_reaches_upstream(self) -> None:
        with patch.object(captions, "list_tracks") as listing:
            self.assertEqual(self.client.get("/api/captions/not-an-id").status_code, 400)
        listing.assert_not_called()

    def test_the_choice_is_saved_with_the_playback_preferences(self) -> None:
        with patch.object(db, "get_playback_preferences", return_value=db.normalize_playback_preferences({})), \
                patch.object(db, "set_playback_preferences", return_value={}) as saved:
            self.client.post("/api/playback/preferences", json={"captions_enabled": True, "captions_language": "pt-BR", "captions_auto": True})
        kwargs = saved.call_args.kwargs
        self.assertEqual((kwargs["captions_enabled"], kwargs["captions_language"], kwargs["captions_auto"]), (True, "pt-BR", True))


class PlaybackPreferenceTests(unittest.TestCase):
    def test_new_keys_default_off(self) -> None:
        prefs = db.normalize_playback_preferences({})
        self.assertEqual((prefs["captions_enabled"], prefs["captions_language"], prefs["captions_auto"], prefs["live_chat_enabled"]), (False, "", False, False))

    def test_a_caption_language_must_look_like_one(self) -> None:
        self.assertEqual(db.normalize_playback_preferences({"captions_language": "es-419"})["captions_language"], "es-419")
        self.assertEqual(db.normalize_playback_preferences({"captions_language": "<script>"})["captions_language"], "")


class PlayerKitTests(unittest.TestCase):
    """The controls the shell adds to every same-origin player."""

    def _shell(self, short: int = 0) -> str:
        with patch.object(main, "_playback_sources", return_value=([{"id": "invidious", "label": "Invidious", "kind": "invidious-local", "url": "/embed/abcdefghijk", "same_origin": "1"}], "")), \
                patch.object(main, "_player_chapters", return_value=[]), \
                patch.object(db, "get_download", return_value=None), \
                patch.object(db, "get_video_by_youtube_id", return_value={"title": 'A "quoted" <title>', "channel_title": "Some Channel"}):
            return main.watch_player(main.Request({"type": "http", "method": "GET", "path": "/", "query_string": b"", "headers": []}), "abcdefghijk", short=short)

    def test_the_shell_loads_the_kit_and_hands_it_title_and_channel(self) -> None:
        page = self._shell()
        self.assertIn("window.OpenFeedPlayerKit = { attach };", page)
        self.assertIn('data-title="A &quot;quoted&quot; &lt;title&gt;"', page)
        self.assertIn('data-channel="Some Channel"', page)
        self.assertIn("window.OpenFeedPlayerKit.attach({", page)

    def test_shorts_get_no_lock_screen_metadata(self) -> None:
        self.assertNotIn("data-title=", self._shell(short=1))

    def test_a_source_switch_drops_the_old_controls(self) -> None:
        script = main._player_script()
        self.assertGreaterEqual(script.count("dropKit();"), 3)
        self.assertIn("openfeed:captions-toggle", script)
        self.assertIn("openfeed:media-queue", script)

    def test_captions_are_tracks_from_this_origin_drawn_by_the_kit(self) -> None:
        self.assertIn("fetch(`/api/captions/${encodeURIComponent(videoId)}`)", KIT)
        self.assertIn("doc.createElement('track')", KIT)
        self.assertIn("'hidden'", KIT)
        self.assertIn("cuechange", KIT)

    def test_c_is_the_toggle_and_the_menu_works_by_keyboard(self) -> None:
        self.assertIn("const CAPTION_KEYS = new Set(['c', 'C']);", KIT)
        self.assertIn("event.stopImmediatePropagation();", KIT)
        for key in ("ArrowDown", "ArrowUp", "Escape", "Home", "End"):
            self.assertIn(f"'{key}'", KIT)
        self.assertIn("'menuitemradio'", KIT)
        self.assertIn("aria-haspopup", KIT)

    def test_auto_generated_tracks_are_labelled(self) -> None:
        self.assertIn("'Auto-generated'", KIT)

    def test_the_choice_is_remembered(self) -> None:
        self.assertIn("captions_language: active.language", KIT)
        self.assertIn("{ captions_enabled: false }", KIT)

    def test_media_session_covers_every_action_and_uses_our_thumbnail_proxy(self) -> None:
        for action in ("play", "pause", "seekbackward", "seekforward", "seekto", "previoustrack", "nexttrack"):
            self.assertIn(f"'{action}'", KIT)
        self.assertIn("setPositionState", KIT)
        self.assertIn("`/vi/${encodeURIComponent(videoId)}/${file}`", KIT)

    def test_casting_is_the_browsers_own_and_never_googles_sdk(self) -> None:
        self.assertIn("remote.prompt()", KIT)
        self.assertIn("webkitShowPlaybackTargetPicker", KIT)
        self.assertIn("watchAvailability", KIT)
        self.assertIn("startsWith('blob:')", KIT)
        for forbidden in ("cast_sender", "gstatic.com", "chrome.cast", "www.google.com"):
            self.assertNotIn(forbidden, KIT)

    def test_the_kit_styles_survive_the_template_literal(self) -> None:
        for forbidden in ("`", "\\", "${"):
            self.assertNotIn(forbidden, MEDIA_CSS)
        self.assertIn(MEDIA_CSS, main._player_chrome_css())

    def test_invidious_s_own_caption_button_gives_way(self) -> None:
        self.assertIn(".of-own-captions .vjs-captions-button", MEDIA_CSS)


class WatchPageMediaTests(unittest.TestCase):
    def test_the_page_forwards_c_and_answers_next_and_previous(self) -> None:
        self.assertIn("type: 'openfeed:captions-toggle'", WATCH_JS)
        self.assertIn("openfeed:media-next", WATCH_JS)
        self.assertIn("window.history.back()", WATCH_JS)
        self.assertIn(WATCH_JS, main._home_script())

    def test_the_chat_panel_only_renders_with_the_setting_on(self) -> None:
        with patch.object(db, "get_playback_preferences", return_value={"live_chat_enabled": False}):
            self.assertEqual(main._chat_panel("abcdefghijk"), "")
        with patch.object(db, "get_playback_preferences", return_value={"live_chat_enabled": True}):
            panel = main._chat_panel("abcdefghijk")
        self.assertIn('data-chat-panel data-video-id="abcdefghijk"', panel)
        self.assertIn(" hidden>", panel)

    def test_the_setting_states_what_it_sends(self) -> None:
        with patch.object(ytdlp, "available", return_value=True), patch.object(db, "get_playback_preferences", return_value={"live_chat_enabled": False}):
            html = main._live_chat_settings_html()
        self.assertIn("data-setting-live-chat", html)
        self.assertIn("Privacy trade-off", html)
        self.assertIn("no cookies, no account", html)
        self.assertNotIn(" checked", html)

    def test_without_ytdlp_the_setting_says_why_it_is_missing(self) -> None:
        with patch.object(ytdlp, "available", return_value=False):
            html = main._live_chat_settings_html()
        self.assertNotIn("data-setting-live-chat", html)
        self.assertIn("Needs yt-dlp", html)


def _text_line(offset: int, author: str, runs: list, **extra) -> str:
    renderer = {"id": f"id-{author}-{offset}", "authorName": {"simpleText": author}, "message": {"runs": runs}, **extra}
    return json.dumps({"replayChatItemAction": {"actions": [{"addChatItemAction": {"item": {"liveChatTextMessageRenderer": renderer}}}]}, "videoOffsetTimeMsec": str(offset)})


class ChatParsingTests(unittest.TestCase):
    def test_a_text_message(self) -> None:
        [message] = livechat.parse_line(_text_line(1500, "@ann", [{"text": "hello "}, {"text": "all"}]))
        self.assertEqual((message["t"], message["author"], message["text"], message["kind"]), (1500, "@ann", "hello all", "text"))

    def test_emoji_become_text_and_custom_emoji_their_shortcut(self) -> None:
        runs = [{"text": "hi "}, {"emoji": {"emojiId": "😀", "isCustomEmoji": False}}, {"emoji": {"emojiId": "UCx/abc", "isCustomEmoji": True, "shortcuts": [":yt:"], "image": {"thumbnails": [{"url": "https://yt3.ggpht.com/x"}]}}}]
        [message] = livechat.parse_line(_text_line(0, "@b", runs))
        self.assertEqual(message["text"], "hi 😀:yt:")
        self.assertNotIn("ggpht", json.dumps(message))

    def test_no_author_photo_is_kept(self) -> None:
        line = _text_line(0, "@c", [{"text": "x"}], authorPhoto={"thumbnails": [{"url": "https://yt4.ggpht.com/p"}]})
        self.assertNotIn("ggpht", json.dumps(livechat.parse_line(line)))

    def test_roles_come_from_badges(self) -> None:
        badges = [{"liveChatAuthorBadgeRenderer": {"icon": {"iconType": "MODERATOR"}}}, {"liveChatAuthorBadgeRenderer": {"customThumbnail": {}}}]
        [message] = livechat.parse_line(_text_line(0, "@m", [{"text": "x"}], authorBadges=badges))
        self.assertEqual(message["role"], "moderator")

    def test_paid_messages_carry_the_amount(self) -> None:
        renderer = {"id": "p", "authorName": {"simpleText": "@d"}, "purchaseAmountText": {"simpleText": "$5.00"}, "message": {"runs": [{"text": "thanks"}]}}
        line = json.dumps({"replayChatItemAction": {"actions": [{"addChatItemAction": {"item": {"liveChatPaidMessageRenderer": renderer}}}]}, "videoOffsetTimeMsec": "10"})
        [message] = livechat.parse_line(line)
        self.assertEqual((message["kind"], message["amount"], message["text"]), ("paid", "$5.00", "thanks"))

    def test_gift_view_models_are_read(self) -> None:
        item = {"giftMessageViewModel": {"id": "g", "text": {"content": "sent Treat"}, "authorName": {"content": "@e "}}}
        line = json.dumps({"replayChatItemAction": {"actions": [{"addChatItemAction": {"item": item}}]}, "isLive": True})
        [message] = livechat.parse_line(line)
        self.assertEqual((message["author"], message["text"], message["t"]), ("@e", "sent Treat", None))

    def test_placeholders_banners_and_garbage_yield_nothing(self) -> None:
        placeholder = json.dumps({"replayChatItemAction": {"actions": [{"addChatItemAction": {"item": {"liveChatPlaceholderItemRenderer": {"id": "x"}}}}, {"addBannerToLiveChatCommand": {}}]}})
        self.assertEqual(livechat.parse_line(placeholder), [])
        self.assertEqual(livechat.parse_line("not json"), [])
        self.assertEqual(livechat.parse_line("[]"), [])


class ChatStatusTests(unittest.TestCase):
    def _status(self, info=None, error=None, available=True) -> dict:
        probe = patch.object(ytdlp, "probe", side_effect=error) if error else patch.object(ytdlp, "probe", return_value=info)
        with patch.object(ytdlp, "available", return_value=available), probe:
            return livechat.status("abcdefghijk")

    def test_live_with_chat(self) -> None:
        self.assertEqual(self._status({"live_status": "is_live", "subtitles": {"live_chat": [{}]}})["mode"], "live")

    def test_a_past_stream_has_a_replay(self) -> None:
        self.assertEqual(self._status({"live_status": "was_live", "subtitles": {"live_chat": [{}]}})["mode"], "replay")

    def test_a_past_stream_without_chat_says_so(self) -> None:
        result = self._status({"live_status": "was_live", "subtitles": {}})
        self.assertEqual(result["mode"], "none")
        self.assertIn("no chat replay", result["reason"])

    def test_an_ordinary_video_has_no_chat(self) -> None:
        self.assertEqual(self._status({"live_status": "not_live", "subtitles": {"en": []}})["mode"], "none")

    def test_a_premiere_that_has_not_started(self) -> None:
        self.assertEqual(self._status(error=ytdlp.YtdlpError("Premieres in 3 hours"))["mode"], "upcoming")

    def test_without_ytdlp_nothing_is_fetched(self) -> None:
        with patch.object(ytdlp, "probe") as probe:
            result = self._status(available=False)
        self.assertEqual(result["mode"], "none")
        probe.assert_not_called()

    def test_chat_is_read_without_the_operators_cookies(self) -> None:
        with patch.object(ytdlp, "base_args", return_value=["--no-playlist", "--cookies", "/secret/cookies.txt", "--proxy", "socks5://p"]):
            args = livechat._chat_args()
        self.assertNotIn("--cookies", args)
        self.assertNotIn("/secret/cookies.txt", args)
        self.assertIn("--proxy", args)


class ChatApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(main.app)

    def test_off_by_default(self) -> None:
        with patch.object(db, "get_playback_preferences", return_value=db.normalize_playback_preferences({})), patch.object(livechat, "status") as status:
            response = self.client.get("/api/livechat/abcdefghijk")
        self.assertEqual(response.status_code, 403)
        status.assert_not_called()

    def test_with_the_setting_on(self) -> None:
        prefs = db.normalize_playback_preferences({"live_chat_enabled": True})
        with patch.object(db, "get_playback_preferences", return_value=prefs), patch.object(livechat, "status", return_value={"mode": "replay", "reason": ""}):
            self.assertEqual(self.client.get("/api/livechat/abcdefghijk").json()["mode"], "replay")
        with patch.object(db, "get_playback_preferences", return_value=prefs), patch.object(livechat, "replay", return_value={"state": "ready", "messages": []}) as replay:
            self.client.get("/api/livechat/abcdefghijk/replay?start=60&end=240")
        self.assertEqual(replay.call_args.args, ("abcdefghijk", 60.0, 240.0))


def _wait(predicate, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline and not predicate():
        time.sleep(0.02)


class ChatReplayAndLiveTests(unittest.TestCase):
    def setUp(self) -> None:
        livechat.reset()

    def tearDown(self) -> None:
        livechat.reset()

    def test_a_replay_is_windowed_by_video_time(self) -> None:
        def follow(video_id, on_messages, should_stop, timeout):
            on_messages([{"id": str(t), "t": t * 1000, "author": "a", "text": str(t), "kind": "text", "amount": "", "role": ""} for t in (5, 50, 200, 400)])
            on_messages([{"id": "5", "t": 5000, "author": "a", "text": "dup", "kind": "text", "amount": "", "role": ""}])
            return "ended"

        with patch.object(livechat, "_follow", side_effect=follow):
            self.assertEqual(livechat.replay("abcdefghijk")["state"], "loading")
            _wait(lambda: livechat.replay("abcdefghijk")["state"] == "ready")
            window = livechat.replay("abcdefghijk", 40, 220)
        self.assertEqual([m["text"] for m in window["messages"]], ["50", "200"])
        self.assertEqual((window["count"], window["through"], window["partial"]), (4, 400.0, False))

    def test_a_replay_that_runs_out_of_time_is_marked_partial(self) -> None:
        def follow(video_id, on_messages, should_stop, timeout):
            on_messages([{"id": "1", "t": 1000, "author": "a", "text": "x", "kind": "text", "amount": "", "role": ""}])
            return "timeout"

        with patch.object(livechat, "_follow", side_effect=follow):
            livechat.replay("abcdefghijk")
            _wait(lambda: livechat.replay("abcdefghijk")["state"] == "ready")
            self.assertTrue(livechat.replay("abcdefghijk")["partial"])

    def test_live_messages_are_numbered_and_resumable(self) -> None:
        def follow(video_id, on_messages, should_stop, timeout):
            on_messages([{"id": f"m{n}", "t": None, "author": "a", "text": str(n), "kind": "text", "amount": "", "role": ""} for n in range(3)])
            return "ended"

        with patch.object(livechat, "_follow", side_effect=follow):
            livechat.live("abcdefghijk")
            _wait(lambda: livechat.live("abcdefghijk")["state"] == "ended")
            everything = livechat.live("abcdefghijk", 0)
            newer = livechat.live("abcdefghijk", 2)
        self.assertEqual([m["seq"] for m in everything["messages"]], [1, 2, 3])
        self.assertEqual([m["text"] for m in newer["messages"]], ["2"])

    def test_the_number_of_followed_streams_is_capped(self) -> None:
        def follow(video_id, on_messages, should_stop, timeout):
            _wait(should_stop, 2.0)
            return "stopped"

        with patch.object(livechat, "_follow", side_effect=follow), patch.object(livechat, "LIVE_MAX_SESSIONS", 1), patch.object(livechat, "LIVE_IDLE_SECONDS", 0.2):
            self.assertNotEqual(livechat.live("aaaaaaaaaaa")["state"], "busy")
            self.assertEqual(livechat.live("bbbbbbbbbbb")["state"], "busy")


class FollowTests(unittest.TestCase):
    """_follow against a stand-in yt-dlp that writes a growing .part file and renames it."""

    def test_messages_arrive_while_the_file_is_still_being_written(self) -> None:
        directory = tempfile.mkdtemp()
        fake = Path(directory) / "fake-yt-dlp"
        lines = [_text_line(1000 * n, f"@u{n}", [{"text": f"msg {n}"}]) for n in range(4)]
        fake.write_text(
            f"#!{sys.executable}\n"
            "import sys, time, os\n"
            "out = sys.argv[sys.argv.index('-o') + 1] + '.live_chat.json'\n"
            f"lines = {lines!r}\n"
            "with open(out + '.part', 'w') as handle:\n"
            "    for line in lines:\n"
            "        handle.write(line + '\\n'); handle.flush(); time.sleep(0.3)\n"
            "os.rename(out + '.part', out)\n",
            encoding="utf-8",
        )
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        batches: list[list[dict]] = []
        with patch.object(ytdlp, "binary_path", return_value=str(fake)), patch.object(ytdlp, "base_args", return_value=[]):
            outcome = livechat._follow("abcdefghijk", batches.append, lambda: False, 20)
        self.assertEqual(outcome, "ended")
        self.assertGreater(len(batches), 1)
        self.assertEqual([m["text"] for batch in batches for m in batch], ["msg 0", "msg 1", "msg 2", "msg 3"])
        os.remove(fake)
        os.rmdir(directory)

    def test_a_failing_run_reports_ytdlps_error(self) -> None:
        directory = tempfile.mkdtemp()
        fake = Path(directory) / "fake-yt-dlp"
        fake.write_text(f"#!{sys.executable}\nimport sys\nsys.stderr.write('ERROR: [youtube] x: Video unavailable\\n')\nsys.exit(1)\n", encoding="utf-8")
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        with patch.object(ytdlp, "binary_path", return_value=str(fake)), patch.object(ytdlp, "base_args", return_value=[]):
            with self.assertRaises(RuntimeError) as caught:
                livechat._follow("abcdefghijk", lambda batch: None, lambda: False, 20)
        self.assertIn("Video unavailable", str(caught.exception))
        os.remove(fake)
        os.rmdir(directory)


if __name__ == "__main__":
    unittest.main()
