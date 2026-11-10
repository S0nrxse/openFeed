"""Appearance: the light token set, the first-paint head script and the stored choice."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from fastapi.testclient import TestClient

from openfeed import appearance, db, main

ASSETS = Path(main.__file__).parent / "assets"
TOKENS = (ASSETS / "css" / "00-tokens.css").read_text(encoding="utf-8")


def _block(css: str, selector: str) -> str:
    """The declarations inside the first rule whose selector is exactly `selector`."""
    start = css.index(selector + " {") + len(selector) + 2
    return css[start:css.index("}", start)]


def _pairs(declarations: str) -> dict[str, str]:
    return {name: value.strip() for name, value in re.findall(r"(--[a-z0-9-]+):\s*([^;]+);", declarations)}


DARK = _pairs(_block(TOKENS, ":root"))
LIGHT = _pairs(_block(TOKENS, ':root[data-theme="light"]'))
SYSTEM = _pairs(_block(TOKENS[TOKENS.index("@media (prefers-color-scheme: light)"):], ':root[data-theme="system"]'))


class TokenParityTests(unittest.TestCase):
    def test_device_theme_uses_exactly_the_light_set(self) -> None:
        self.assertGreater(len(LIGHT), 30)
        self.assertEqual(LIGHT, SYSTEM)
        self.assertIn("color-scheme: light;", _block(TOKENS, ':root[data-theme="light"]'))
        self.assertIn("color-scheme: light;", _block(TOKENS[TOKENS.index("@media (prefers-color-scheme: light)"):], ':root[data-theme="system"]'))

    def test_light_only_overrides_tokens_that_exist_in_dark(self) -> None:
        self.assertEqual(set(LIGHT) - set(DARK), set())

    def test_every_colour_token_has_a_light_value(self) -> None:
        # Anything that is a colour, or a shadow, ring or scrim built from one, must be
        # restated for light, apart from the overlays that stay dark on video in both themes.
        stays_dark = {"--badge"} | {name for name in DARK if name.startswith("--over-video-")}
        coloured = {name for name, value in DARK.items() if re.search(r"#[0-9a-f]{3,8}\b|rgba?\(|\d+,\d+,\d+|url\(", value)}
        self.assertEqual(coloured - stays_dark - set(LIGHT), set())
        for name in stays_dark:
            self.assertNotIn(name, LIGHT)

    def test_dark_keeps_its_measured_values(self) -> None:
        self.assertEqual(DARK["--bg"], "#0a0a0b")
        self.assertEqual(DARK["--text"], "#ededef")
        self.assertEqual(DARK["--focus"], "0 0 0 2px rgba(255,255,255,.5)")
        self.assertIn("color-scheme: dark;", _block(TOKENS, ":root"))

    def test_the_inverse_and_the_accent(self) -> None:
        # Selection still inverts: near-black fill, white text. The accent is the same red.
        self.assertEqual(LIGHT["--bone"], LIGHT["--text"])
        self.assertEqual(LIGHT["--on-bone"], LIGHT["--bg"])
        self.assertEqual(LIGHT["--accent"], DARK["--accent"])

    def test_the_browser_chrome_colour_is_the_page_colour(self) -> None:
        self.assertEqual(appearance.THEME_COLORS["dark"], DARK["--bg"])
        self.assertEqual(appearance.THEME_COLORS["light"], LIGHT["--bg"])

    def test_the_sign_in_page_carries_the_same_light_values(self) -> None:
        css = main._AUTH_PAGE_CSS
        light = _pairs(_block(css, ':root[data-theme="light"]'))
        system = _pairs(_block(css[css.index("@media (prefers-color-scheme: light)"):], ':root[data-theme="system"]'))
        self.assertGreater(len(light), 10)
        self.assertEqual(light, system)
        for name, value in light.items():
            self.assertEqual(LIGHT.get(name), value, name)

    def test_page_chrome_reads_tokens_not_dark_literals(self) -> None:
        css = main._home_styles()
        for selector in (".app-header", ".mobile-bottom-nav", ".day-head"):
            rules = re.findall(r"(?m)^\s*" + re.escape(selector) + r" \{[^}]*\}", css)
            self.assertTrue(rules, selector)
            for rule in rules:
                self.assertNotIn("rgba(10,10,11", rule, selector)
        select = css[css.index(":is(.select"):css.index("}", css.index(":is(.select"))]
        self.assertNotIn("color-scheme", select)
        self.assertIn("var(--select-chevron)", css)
        self.assertIn("var(--thumb-bed)", css)


class _Store:
    """Just enough of users.profile for appearance.get_choice and save_choice."""

    def __init__(self, profile: dict[str, Any] | None = None) -> None:
        self.profile = profile
        self.calls: list[tuple[str, Any]] = []
        self._row: Any = None

    def __enter__(self) -> "_Store":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def cursor(self) -> "_Store":
        return self

    def commit(self) -> None:
        return None

    def execute(self, query: str, params: Any = None) -> None:
        self.calls.append((query, params))
        if query.lstrip().startswith("INSERT INTO users"):
            if self.profile is None:
                self.profile = {}
            self._row = {"id": "user-1"}
        elif query.startswith("SELECT profile->>'appearance'"):
            self._row = None if self.profile is None else {"appearance": self.profile.get("appearance")}
        elif query.startswith("UPDATE users SET profile"):
            self.profile = {**(self.profile or {}), "appearance": params[0]}
            self._row = None

    def fetchone(self) -> Any:
        return self._row


class StoredChoiceTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        store = _Store({"show_comments": True})
        with patch.object(db, "connect", return_value=store):
            self.assertEqual(appearance.get_choice("alice"), "")
            self.assertEqual(appearance.save_choice("Light", "alice"), "light")
            self.assertEqual(appearance.get_choice("alice"), "light")
        # One key changes; the rest of the profile is left alone by a jsonb merge.
        self.assertEqual(store.profile, {"show_comments": True, "appearance": "light"})
        update = next(query for query, _ in store.calls if query.startswith("UPDATE"))
        self.assertIn("|| jsonb_build_object('appearance', %s::text)", update)

    def test_reading_never_creates_a_user(self) -> None:
        store = _Store(None)
        with patch.object(db, "connect", return_value=store):
            self.assertEqual(appearance.get_choice("nobody"), "")
        self.assertFalse(any("INSERT" in query for query, _ in store.calls))

    def test_an_unknown_value_is_refused_before_the_database(self) -> None:
        store = _Store({})
        with patch.object(db, "connect", return_value=store):
            with self.assertRaises(ValueError):
                appearance.save_choice("sepia", "alice")
        self.assertEqual(store.calls, [])

    def test_a_stored_value_that_is_not_a_choice_reads_as_none(self) -> None:
        with patch.object(db, "connect", return_value=_Store({"appearance": "<script>"})):
            self.assertEqual(appearance.get_choice("alice"), "")

    def test_a_database_failure_reads_as_none(self) -> None:
        with patch.object(db, "connect", side_effect=RuntimeError("down")):
            self.assertEqual(appearance.get_choice("alice"), "")


class AppearanceApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(main.app)

    def test_put_then_get(self) -> None:
        store = _Store({})
        with patch.object(db, "connect", return_value=store):
            saved = self.client.put("/api/settings/appearance", json={"appearance": "system"})
            read = self.client.get("/api/settings/appearance")
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(saved.json(), {"ok": True, "appearance": "system"})
        self.assertEqual(read.json()["appearance"], "system")
        self.assertTrue(read.json()["stored"])

    def test_nothing_stored_reads_as_the_dark_default(self) -> None:
        with patch.object(db, "connect", return_value=_Store(None)):
            body = self.client.get("/api/settings/appearance").json()
        self.assertEqual((body["appearance"], body["stored"]), ("dark", False))
        self.assertEqual(body["choices"], ["dark", "light", "system"])

    def test_an_unknown_theme_is_a_400(self) -> None:
        store = _Store({})
        with patch.object(db, "connect", return_value=store):
            response = self.client.put("/api/settings/appearance", json={"appearance": "sepia"})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(any("appearance" in query for query, _ in store.calls))


class HeadScriptTests(unittest.TestCase):
    def test_shell_head_sets_the_theme_before_the_stylesheet(self) -> None:
        head = main._shell_head("light")
        script = head.index("<script>(function(){var d=document.documentElement")
        self.assertLess(script, head.index('rel="stylesheet"'))
        # The metas come first so the script can point them at the chosen theme.
        self.assertLess(head.index('<meta name="theme-color" content="#0a0a0b" media="(prefers-color-scheme: dark)">'), script)
        self.assertLess(head.index('<meta name="theme-color" content="#ffffff" media="(prefers-color-scheme: light)">'), script)
        self.assertNotIn('<meta name="theme-color" content="#0a0a0b">', head)
        self.assertIn('c="light"', head)

    def test_the_stored_value_is_normalised_before_it_reaches_the_script(self) -> None:
        self.assertIn('c=""', appearance.head_script('"</script><script>alert(1)//'))
        self.assertIn('c=""', main._shell_head())

    def test_the_sign_in_error_page_runs_it_too(self) -> None:
        html = main._auth_error_page("Title", "Cause").body.decode()
        self.assertIn("localStorage.getItem('openfeed-theme')", html)
        self.assertLess(html.index("openfeed-theme"), html.index("<style>"))

    def _run(self, stored: str, local: str | None = None, cookie: str = "") -> dict[str, Any]:
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        body = appearance.head_script(stored).removeprefix("<script>").removesuffix("</script>")
        program = (
            "const metas=[{content:'#0a0a0b',media:'(prefers-color-scheme: dark)'},{content:'#ffffff',media:'(prefers-color-scheme: light)'}];"
            "const attrs={};"
            "globalThis.document={documentElement:{setAttribute:(k,v)=>{attrs[k]=v}},cookie:" + json.dumps(cookie) + ","
            "querySelectorAll:()=>metas};"
            "globalThis.localStorage={getItem:()=>" + json.dumps(local) + "};"
            + body + ";console.log(JSON.stringify({theme:attrs['data-theme'],metas:metas.map(m=>m.content)}))"
        )
        result = subprocess.run([node, "-e", program], capture_output=True, text=True, timeout=20, check=True)
        return json.loads(result.stdout)

    def test_the_server_choice_wins(self) -> None:
        self.assertEqual(self._run("light", local="dark"), {"theme": "light", "metas": ["#ffffff", "#ffffff"]})

    def test_the_browser_mirror_covers_pages_that_know_no_user(self) -> None:
        self.assertEqual(self._run("", local="light")["theme"], "light")
        self.assertEqual(self._run("", local=None, cookie="a=1; openfeed_theme=light")["theme"], "light")

    def test_device_theme_leaves_the_metas_to_their_media_queries(self) -> None:
        self.assertEqual(self._run("system"), {"theme": "system", "metas": ["#0a0a0b", "#ffffff"]})

    def test_nothing_chosen_is_dark(self) -> None:
        self.assertEqual(self._run("", local=None), {"theme": "dark", "metas": ["#0a0a0b", "#0a0a0b"]})
        self.assertEqual(self._run("", local="nonsense")["theme"], "dark")


class ControlsTests(unittest.TestCase):
    def test_account_menu_row_names_the_current_theme(self) -> None:
        row = main._appearance_menu_row("light")
        self.assertIn("Appearance: <span data-appearance-label>Light</span>", row)
        self.assertIn('aria-haspopup="true"', row)
        self.assertIn("Appearance: <span data-appearance-label>Dark</span>", main._appearance_menu_row(""))

    def test_menu_page_offers_three_radios_in_youtube_order(self) -> None:
        page = main._appearance_menu_page("system")
        options = re.findall(r'data-appearance-option="(\w+)" aria-checked="(\w+)"', page)
        self.assertEqual(options, [("system", "true"), ("dark", "false"), ("light", "false")])
        self.assertIn('role="menuitemradio"', page)
        self.assertIn('aria-label="Back to account menu"', page)

    def test_settings_pane_is_a_radio_group_with_the_stored_choice(self) -> None:
        with patch.object(main, "_appearance_choice", return_value="light"):
            pane = main._appearance_pane("alice")
        self.assertIn('role="radiogroup" aria-label="Appearance"', pane)
        checked = re.findall(r'value="(\w+)" data-appearance-option="\w+"( checked)?', pane)
        self.assertEqual(checked, [("system", ""), ("dark", ""), ("light", " checked")])

    def test_the_header_and_settings_carry_the_controls(self) -> None:
        with patch.object(main, "_appearance_choice", return_value="dark"):
            header = main._header_actions({"authenticated": False}, "local")
        self.assertIn("data-appearance-open", header)
        self.assertIn("data-appearance-page", header)
        self.assertLess(header.index("data-appearance-open"), header.index('<span class="menu-label">Settings</span>'))

    def test_the_script_is_in_the_shell_bundle(self) -> None:
        script = main._home_script()
        self.assertIn("window.ofSetAppearance", script)
        self.assertIn("'/api/settings/appearance'", script)
        self.assertIn("openfeed-theme", script)


if __name__ == "__main__":
    unittest.main()
