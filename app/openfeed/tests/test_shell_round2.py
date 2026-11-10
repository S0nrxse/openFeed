"""Shell defects from the second QA sweep. Each test names the failure it stops.

The behaviour itself is JavaScript and was checked in a browser (real touch, mouse and
keyboard); these tests pin the served hooks and the server side of hidden shelves.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from fastapi.testclient import TestClient

from openfeed import db, main
from openfeed.tests.support import offline_db

ASSETS = Path(main.__file__).parent / "assets"
UI_KIT = (ASSETS / "js" / "ui-kit.js").read_text(encoding="utf-8")
UI_LIBRARY = (ASSETS / "js" / "ui-library.js").read_text(encoding="utf-8")
WATCH_CSS = (ASSETS / "css" / "50-watch.css").read_text(encoding="utf-8")
RESPONSIVE_CSS = (ASSETS / "css" / "90-responsive.css").read_text(encoding="utf-8")


def _z_index(css: str, selector: str) -> int:
    match = re.search(re.escape(selector) + r" \{[^}]*?z-index: (\d+)", css)
    assert match, selector
    return int(match.group(1))


class MenuSheetBackdropTests(unittest.TestCase):
    """A popover ::backdrop takes no taps, so a tap above a menu sheet opened the card under it."""

    def test_a_tap_outside_a_menu_sheet_is_swallowed_before_any_page_handler(self) -> None:
        start = UI_KIT.index("window.addEventListener('click', (event) => {\n    const top = sheets.at(-1);")
        handler = UI_KIT[start:UI_KIT.index("}, true);", start)]
        self.assertIn("panel.contains(event.target)", handler)
        self.assertIn("event.preventDefault();", handler)
        self.assertIn("event.stopPropagation();", handler)
        self.assertIn("dismissSheet(top);", handler)
        # Toasts (Undo) and a dialog opened from the menu stay usable.
        self.assertIn("[data-toast-region], dialog[open]", handler)


class CommentsSheetTests(unittest.TestCase):
    def test_the_sheet_never_starts_above_the_screen_in_landscape(self) -> None:
        # Scrolled down in landscape the unpinned player's bottom edge is negative.
        self.assertIn("Math.max(0, Math.min(bottom, window.innerHeight * 0.55))", main._home_script())
        self.assertIn("top: max(var(--safe-top), var(--sheet-top, 40dvh))", RESPONSIVE_CSS)

    def test_a_drag_from_the_middle_of_the_handle_dismisses(self) -> None:
        script = main._home_script()
        start = script.index("function dragToClose(handle, panel, close) {")
        body = script[start:script.index("/* ---- comments sheet (phone) ---- */", start)]
        # Touch adjustment hands the finger to a sort segment: the handle's box decides.
        self.assertIn("panel.addEventListener('pointerdown'", body)
        self.assertIn("handle.getBoundingClientRect()", body)
        # And the segment must not turn the drag into a page pan that cancels it.
        self.assertIn(".comments-panel .comments-head { touch-action: pan-x; }", RESPONSIVE_CSS)


class AccountSettingsRowTests(unittest.TestCase):
    """Reported as opening on Profiles: the tester tapped "Add a profile", the first match."""

    def test_the_settings_row_opens_the_list_not_a_pane(self) -> None:
        with offline_db():
            header = main._header_actions({"display_name": "Main"}, "local")
        rows = [chunk for chunk in header.split("<button") if 'menu-label">Settings<' in chunk]
        self.assertEqual(len(rows), 1)
        opening = rows[0].split(">", 1)[0]
        self.assertIn("data-open-settings-dialog", opening)
        self.assertNotIn("data-settings-target", opening)


class MenuKeyboardTests(unittest.TestCase):
    def test_escape_returns_focus_to_what_opened_the_menu(self) -> None:
        start = UI_KIT.index("if (event.key !== 'Escape' || event.defaultPrevented) return;")
        handler = UI_KIT[start:UI_KIT.index("}, true);", start)]
        self.assertIn("menu.removeAttribute('open');", handler)
        self.assertIn("returnFocus(menu);", handler)
        self.assertIn("menuOpeners.set(menu, active)", UI_KIT)

    def test_tab_cycles_inside_an_open_menu_and_a_modal_dialog(self) -> None:
        start = UI_KIT.index("if (event.key !== 'Tab'")
        handler = UI_KIT[start:UI_KIT.index("next.focus();", start)]
        self.assertIn("matches(':modal')", handler)
        self.assertIn("menuStops(container)", handler)
        self.assertIn("event.preventDefault();", handler)


class SavePickerRaceTests(unittest.TestCase):
    def test_typing_before_the_list_loads_does_not_throw(self) -> None:
        start = UI_LIBRARY.index("function applyFilter() {")
        self.assertLess(UI_LIBRARY.index("if (!createRow) return;", start), UI_LIBRARY.index("createRow.hidden", start))
        self.assertIn("if (createRow && !createRow.hidden) { create(); return; }", UI_LIBRARY)


class ShortsTests(unittest.TestCase):
    def test_channel_and_title_links_sit_above_the_play_pause_layer(self) -> None:
        self.assertGreater(_z_index(WATCH_CSS, ".shorts-overlay"), _z_index(WATCH_CSS, ".shorts-tap"))
        self.assertIn(".shorts-overlay a { pointer-events: auto; }", WATCH_CSS)

    def test_space_and_k_play_or_pause(self) -> None:
        script = main._home_script()
        self.assertIn("event.key === 'k' || (event.key === ' '", script)
        self.assertIn("function togglePlay()", script)
        self.assertNotIn("event.key === 'ArrowUp' || event.key === 'k'", script)

    def test_mute_keeps_one_name_and_reports_state_through_aria_pressed(self) -> None:
        script = main._home_script()
        start = script.index("function paintMute() {")
        body = script[start:script.index("});", start)]
        self.assertIn("button.setAttribute('aria-label', 'Mute');", body)
        self.assertNotIn("Unmute", body)

    def test_the_off_canvas_sidebar_leaves_the_tab_order(self) -> None:
        script = main._home_script()
        self.assertIn("sidebar.inert = body.classList.contains('nav-collapsed')", script)


class FeedControlsDialogTests(unittest.TestCase):
    def test_closing_without_saving_drops_the_edits(self) -> None:
        script = main._home_script()
        self.assertIn("preferenceForm.reset();", script)
        # Saving moves the reset point forward, before the dialog closes.
        self.assertLess(script.index("commitFormDefaults(preferenceForm);"), script.index("showToast('Feed controls saved'"))

    def test_muting_a_pill_changes_one_key_and_lists_it_under_muted_creators(self) -> None:
        script = main._home_script()
        start = script.index("const chip = event.target.closest('[data-mute-channel]');")
        handler = script[start:script.index("showToast(muted ? 'Creator muted'", start)]
        self.assertIn("/api/feed/muted-channels", handler)
        self.assertIn("syncMutedList(key, name, muted);", handler)
        # Saving the pills as the whole list dropped creators muted from a card menu.
        self.assertNotIn("muted_channels: keys", script)


class _Cursor:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = list(rows)
        self.calls: list[tuple[str, Any]] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, query: str, params: Any = None) -> None:
        self.calls.append((query, params))

    def fetchone(self) -> Any:
        return self.rows.pop(0) if self.rows else None

    def fetchall(self) -> list[Any]:
        return []


class _Connection:
    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def cursor(self) -> _Cursor:
        return self._cursor

    def commit(self) -> None:
        return None


class HiddenShelfTests(unittest.TestCase):
    """"Hide this shelf" only removed the DOM node: no Undo, and a reload brought it back."""

    def test_hiding_adds_one_key_and_showing_takes_it_off(self) -> None:
        cursor = _Cursor([{"id": "u1"}, {"profile": {"hidden_shelves": ["your-regulars"], "muted_channels": ["ucx"]}}])
        with patch.object(db, "connect", return_value=_Connection(cursor)):
            result = db.set_shelf_hidden("Continue-Watching", True, "local")
        self.assertEqual(result, ["your-regulars", "continue-watching"])
        update, params = cursor.calls[-1]
        # Only the one profile key is written, so muted creators and the rest survive.
        self.assertIn("jsonb_build_object('hidden_shelves'", update)
        self.assertEqual(json.loads(params[0]), ["your-regulars", "continue-watching"])

        cursor = _Cursor([{"id": "u1"}, {"profile": {"hidden_shelves": ["your-regulars", "continue-watching"]}}])
        with patch.object(db, "connect", return_value=_Connection(cursor)):
            self.assertEqual(db.set_shelf_hidden("continue-watching", False, "local"), ["your-regulars"])

    def test_preferences_report_the_hidden_shelves(self) -> None:
        cursor = _Cursor([{"id": "u1"}, {"profile": {"hidden_shelves": ["new-subscriptions"]}}, {}])
        with patch.object(db, "connect", return_value=_Connection(cursor)):
            preferences = db.get_feed_preferences("local")
        self.assertEqual(preferences["hidden_shelves"], ["new-subscriptions"])

    def test_the_endpoint_accepts_only_shelves_that_can_be_hidden(self) -> None:
        client = TestClient(main.app)
        with patch.object(db, "set_shelf_hidden", return_value=["continue-watching"]) as setter:
            response = client.post("/api/feed/hidden-shelves", json={"shelf_key": "Continue-Watching", "hidden": True})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["hidden"])
        setter.assert_called_once_with("continue-watching", True, "local")
        self.assertEqual(client.post("/api/feed/hidden-shelves", json={"shelf_key": "recommended"}).status_code, 400)

    def test_a_hidden_shelf_stays_out_of_home_on_every_page(self) -> None:
        sections = [
            {"id": "recommended"},
            {"id": "continue-watching"},
            {"id": "your-regulars-3"},
            {"id": "in-case-you-missed-2"},
        ]
        with patch.object(db, "get_feed_preferences", return_value={"hidden_shelves": ["continue-watching", "your-regulars"]}):
            kept = main._without_hidden_shelves(sections, "local")
        self.assertEqual([section["id"] for section in kept], ["recommended", "in-case-you-missed-2"])
        with patch.object(db, "get_feed_preferences", side_effect=RuntimeError("down")):
            self.assertEqual(main._without_hidden_shelves(sections, "local"), sections)

    def test_the_hide_button_names_its_shelf_and_feed_controls_can_show_it_again(self) -> None:
        shelf = main._shelf_html("your-regulars-4", "Back to the Channels You Watch Most", [], main.get_settings())
        self.assertIn('data-dismiss-shelf="your-regulars"', shelf)
        self.assertNotIn("data-dismiss-shelf", main._shelf_html("something-else", "X", [], main.get_settings()))
        with offline_db(get_feed_preferences={"hidden_shelves": ["continue-watching"]}):
            dialog = main._feed_dialog("local")
        self.assertIn('data-show-shelf="continue-watching"', dialog)
        self.assertIn("Continue watching", dialog)
        with offline_db():
            self.assertIn("data-fc-hidden-shelves hidden", main._feed_dialog("local"))
        script = main._home_script()
        self.assertIn("label: 'Undo'", script[script.index("async function hideShelf(button)"):])


class HistoryCountTests(unittest.TestCase):
    def test_the_header_count_follows_the_filter(self) -> None:
        start = UI_LIBRARY.index("function syncMeta(needle) {")
        body = UI_LIBRARY[start:UI_LIBRARY.index("document.addEventListener('input'", start)]
        self.assertIn("`${shown} of ${base}`", body)
        self.assertIn("syncMeta(event.target.value.trim());", UI_LIBRARY)


class ShortcutsHelpTests(unittest.TestCase):
    def test_question_mark_opens_a_modal_list_outside_text_fields(self) -> None:
        start = UI_KIT.index("if (event.key !== '?'")
        handler = UI_KIT[start:UI_KIT.index("openShortcuts();", start)]
        self.assertIn("input, textarea, select, [contenteditable]", handler)
        self.assertIn("shortcutsDialog.showModal();", UI_KIT)
        self.assertIn("feed-dialog shortcuts-dialog", UI_KIT)

    def test_it_lists_the_keys_the_handlers_answer_to(self) -> None:
        for key in ("'t'", "'Space', 'k'", "'j', 'l'", "'f'", "'c'", "'m'"):
            self.assertIn(key, UI_KIT)
        script = main._home_script()
        # Shorts: the page script's keys, and the theater toggle the watch list names.
        self.assertIn("else if (event.key === 'm') setMuted(!shortsMuted);", script)
        self.assertIn("if (event.key !== 't' && event.key !== 'T') return;", script)


if __name__ == "__main__":
    unittest.main()
