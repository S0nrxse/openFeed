"""Regression tests for the watch-page player fixes (QA sweep, fix round 2).

The player runs inside the Invidious embed, so its behaviour is checked in a browser
(see the fix report); these tests pin the hooks that behaviour hangs on.
"""
import inspect
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi import Response
from fastapi.testclient import TestClient

from openfeed import db, main

ASSETS = Path(main.__file__).parent / "assets"
KIT = (ASSETS / "player" / "kit" / "media-kit.js").read_text(encoding="utf-8")
WATCH_MEDIA = (ASSETS / "js" / "watch-media.js").read_text(encoding="utf-8")
SHELL_JS = (ASSETS / "js" / "shell.js").read_text(encoding="utf-8")


class SpeedButtonTests(unittest.TestCase):
    def test_the_speed_button_opens_its_row_in_both_bars_instead_of_stepping(self) -> None:
        script = main._player_script()
        # Every speed control, not only the phone bar: a desktop click used to set 1.25x.
        self.assertIn("doc.querySelectorAll('.vjs-playback-rate').forEach((rate) => {", script)
        self.assertNotIn("doc.querySelector('.mobile-operations-bar .vjs-playback-rate')", script)
        self.assertIn("if (name === 'click') setOpen(!menu.classList.contains('vjs-lock-showing'));", script)
        self.assertIn("button.setAttribute('aria-expanded', open ? 'true' : 'false');", script)


class KeyboardTests(unittest.TestCase):
    def test_the_kit_catches_shortcuts_before_video_js_controls_swallow_them(self) -> None:
        self.assertIn("const keys = setupKeys(context);", KIT)
        self.assertIn("key: keys.run,", KIT)
        # Capture phase on the embed window, and nothing after it sees a handled key.
        self.assertIn("event.stopImmediatePropagation();\n    }, true);\n    return { run };", KIT)
        for key in ("case ' ': case 'k':", "case 'j':", "case 'l':", "case 'ArrowLeft':", "case 'm':", "case 'f':"):
            self.assertIn(key, KIT)

    def test_shift_n_asks_the_page_for_up_next_instead_of_invidious_next_video(self) -> None:
        self.assertIn("case 'N': post({ type: 'openfeed:media-next' }); return true;", KIT)
        self.assertNotIn("action = next_video", KIT)

    def test_t_inside_the_player_reaches_the_page_theater_toggle(self) -> None:
        self.assertIn("case 't': case 'T': post({ type: 'openfeed:theater-toggle' }); return true;", KIT)
        self.assertIn("type === 'openfeed:theater-toggle'", WATCH_MEDIA)
        self.assertIn("document.querySelector('[data-theater-toggle]')?.click();", WATCH_MEDIA)

    def test_keys_pressed_on_the_page_are_forwarded_to_the_player(self) -> None:
        self.assertIn("tell({ type: 'openfeed:key', key });", WATCH_MEDIA)
        self.assertIn("data.type === 'openfeed:key'", main._player_script())
        self.assertIn("kit.key(data.key)", main._player_script())

    def test_fields_keep_their_keys(self) -> None:
        self.assertIn("if (typingIn(target)) return;", KIT)
        self.assertIn("typing(event.target)) return;", WATCH_MEDIA)


class StallTests(unittest.TestCase):
    def test_a_start_that_never_moves_offers_the_next_source(self) -> None:
        script = main._player_script()
        self.assertIn("const STALL_DEADLINE = 12000;", script)
        self.assertIn("watchStall(video);", script)
        self.assertIn("nextButton.textContent = `Try ${upcoming.label}`;", script)
        # With nowhere else to go, the failure card says what happened.
        self.assertIn("endAttempt(source, 'failed', why);", script)

    def test_the_shell_carries_the_stall_notice(self) -> None:
        source = inspect.getsource(main.watch_player)
        for hook in ('data-stall role="alert" hidden', "data-stall-next", "data-stall-wait"):
            self.assertIn(hook, source)


class QualityMenuTests(unittest.TestCase):
    def test_the_cog_is_named_and_the_choice_is_marked(self) -> None:
        self.assertIn("const quality = setupQuality(context);", KIT)
        self.assertIn("`Quality: ${chosen}`", KIT)
        self.assertIn("button.dataset.tip = 'Quality'", KIT)
        self.assertIn("item.setAttribute('aria-checked', String(selected))", KIT)
        # All renditions enabled is what Auto means.
        self.assertIn("enabled.length === all.length) return 'Auto';", KIT)


class ResumeNoticeTests(unittest.TestCase):
    def test_the_notice_is_in_the_page_with_a_close_control_not_a_toast(self) -> None:
        source = inspect.getsource(main.watch_video)
        self.assertIn('class="resume-note" data-resume-note role="status"', source)
        self.assertIn("data-resume-dismiss", source)
        self.assertNotIn("showToast(`Resuming from", main._home_script())
        self.assertIn(".resume-note {", main._home_styles())


class MarkUnwatchedTests(unittest.TestCase):
    def test_the_event_is_allowed(self) -> None:
        self.assertIn("mark_unwatched", main.ALLOWED_FEEDBACK_EVENTS)

    def test_the_menu_row_offers_the_opposite_of_the_state(self) -> None:
        unwatched = main._watched_menu_row("vid1", False)
        watched = main._watched_menu_row("vid1", True)
        self.assertIn('data-feedback="mark_watched"', unwatched)
        self.assertIn("Mark watched", unwatched)
        self.assertIn('data-feedback="mark_unwatched"', watched)
        self.assertIn("Mark unwatched", watched)
        self.assertIn("data-watched-toggle", watched)

    def test_card_and_watch_menus_follow_the_state(self) -> None:
        self.assertIn('data-feedback="mark_unwatched"', main._video_action_menu("vid1", "T", "/watch/vid1", "https://yt", watched=True))
        self.assertIn('data-feedback="mark_watched"', main._video_action_menu("vid1", "T", "/watch/vid1", "https://yt"))
        self.assertIn('data-feedback="mark_unwatched"', main._watch_options_menu("vid1", "", "https://yt", watched=True))
        self.assertIn("watched=_is_watched(progress_row)", inspect.getsource(main.watch_video))

    def test_the_page_flips_the_rows_and_the_card(self) -> None:
        self.assertIn("window.ofMarkUnwatched = (videoId) => {", SHELL_JS)
        self.assertIn("row.dataset.feedback = watched ? 'mark_unwatched' : 'mark_watched';", SHELL_JS)
        self.assertIn("window.ofMarkUnwatched(videoId);", main._home_script())

    def test_unwatching_clears_the_watched_state_and_the_watched_events(self) -> None:
        cursor = MagicMock()
        cursor.__enter__.return_value = cursor
        cursor.fetchone.return_value = {"id": 7}
        connection = MagicMock()
        connection.__enter__.return_value = connection
        connection.cursor.return_value = cursor
        with patch.object(db, "connect", return_value=connection):
            db.set_video_feedback("vid1", "mark_unwatched", {}, "owner-1")
        statements = [call.args[0] for call in cursor.execute.call_args_list]
        update = next(sql for sql in statements if "UPDATE playback_progress" in sql)
        for column in ("completed = false", "manually_marked_watched = false", "percent_completed = 0"):
            self.assertIn(column, update)
        # The resume mark is a real reading and stays.
        self.assertNotIn("resume_seconds", update)
        delete = next(sql for sql in statements if sql.startswith("DELETE FROM user_video_events"))
        self.assertIn("event_type = 'mark_watched'", delete)
        delete_call = next(call for call in cursor.execute.call_args_list if call.args[0] == delete)
        self.assertEqual(delete_call.args[1], ("owner-1", 7))
        connection.commit.assert_called_once()

    def test_marking_watched_is_unchanged(self) -> None:
        cursor = MagicMock()
        cursor.__enter__.return_value = cursor
        cursor.fetchone.return_value = {"id": 7}
        connection = MagicMock()
        connection.__enter__.return_value = connection
        connection.cursor.return_value = cursor
        with patch.object(db, "connect", return_value=connection):
            db.set_video_feedback("vid1", "mark_watched", {}, "owner-1")
        statements = [call.args[0] for call in cursor.execute.call_args_list]
        self.assertFalse(any("DELETE FROM user_video_events" in sql for sql in statements))
        self.assertTrue(any("manually_marked_watched = true" in sql for sql in statements))


class LikeLabelTests(unittest.TestCase):
    def test_the_name_follows_the_pressed_state(self) -> None:
        source = inspect.getsource(main.watch_video)
        self.assertIn('like_label = escape(("Liked" if reaction == "like" else "Like") + like_count_note, quote=True)', source)
        self.assertIn('data-label-note=', source)
        script = main._home_script()
        self.assertIn("button.setAttribute('aria-label', `${word}${button.dataset.labelNote || ''}`);", script)
        self.assertIn("button.dataset.tip = word;", script)

    def test_the_count_is_named_as_youtubes_and_never_bumped(self) -> None:
        self.assertIn('like_count_note = f", {like_total} likes on YouTube"', inspect.getsource(main.watch_video))


class DownloadDialogTests(unittest.TestCase):
    def test_nothing_to_download_hides_the_destination_cards(self) -> None:
        script = main._home_script()
        self.assertIn("dialog.querySelector('[data-download-destination]').hidden = true;", script)


class StoryboardTests(unittest.TestCase):
    def test_a_missing_storyboard_is_an_empty_track_not_a_404(self) -> None:
        client = TestClient(main.app)
        with patch.object(main, "proxy_invidious", return_value=Response(status_code=404)) as proxied:
            for url in ("/embed/api/v1/storyboards/abc?height=90", "/api/v1/storyboards/abc?height=90"):
                response = client.get(url)
                self.assertEqual(response.status_code, 200, url)
                self.assertTrue(response.headers["content-type"].startswith("text/vtt"))
                self.assertEqual(response.text, "WEBVTT\n")
        self.assertEqual(proxied.call_args[0][1], "api/v1/storyboards/abc")
        self.assertTrue(proxied.call_args.kwargs.get("answer_missing_now"))

    def test_the_bare_listing_still_404s(self) -> None:
        with patch.object(main, "proxy_invidious", return_value=Response(status_code=404)):
            self.assertEqual(TestClient(main.app).get("/api/v1/storyboards/abc").status_code, 404)

    def test_a_real_storyboard_passes_through(self) -> None:
        with patch.object(main, "proxy_invidious", return_value=Response("WEBVTT\n\n00:00.000 --> 00:10.000\nx.jpg", status_code=200)):
            self.assertIn("x.jpg", TestClient(main.app).get("/embed/api/v1/storyboards/abc?height=90").text)


if __name__ == "__main__":
    unittest.main()
