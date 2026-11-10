"""The shared UI layer: stylesheet files, hashed shell assets and the component helpers."""

import re
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from openfeed import main
from openfeed.tests.support import offline_db

ASSETS = Path(main.__file__).parent / "assets"


def _css_rule(css: str, selector: str) -> str:
    start = css.index(f"\n{selector} {{") + 1
    return css[start:css.index("}", start) + 1]


class StylesheetFileTests(unittest.TestCase):
    def test_the_stylesheet_is_the_css_files_in_name_order(self) -> None:
        # The numeric prefix is the cascade: a rule's position is part of its meaning.
        files = sorted((ASSETS / "css").glob("*.css"))
        self.assertTrue(files)
        self.assertTrue(all(re.match(r"\d\d-", path.name) for path in files))
        self.assertEqual(main._home_styles(), "".join(path.read_text(encoding="utf-8") for path in files))

    def test_an_unchanged_stylesheet_is_not_reread(self) -> None:
        self.assertIs(main._home_styles(), main._home_styles())

    def test_the_responsive_rules_stay_last(self) -> None:
        css = main._home_styles()
        self.assertGreater(css.index("/* ---------- responsive ---------- */"), css.index("/* ---------- theater mode ---------- */"))

    def test_the_css_files_are_shipped_by_the_image(self) -> None:
        path = Path(main.__file__).parents[1] / "Dockerfile"
        if not path.is_file():
            self.skipTest("the Dockerfile is not inside the image; run from a checkout to check it")
        dockerfile = path.read_text(encoding="utf-8")
        self.assertIn("COPY openfeed ./openfeed", dockerfile)


class ShellAssetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(main.app)
        self.addCleanup(self.client.close)

    def _urls(self) -> tuple[str, str]:
        with offline_db():
            html = main._shell_head() + main._shell_foot()
        return (
            re.search(r'href="(/assets/shell/[0-9a-f]{16}\.css)"', html).group(1),
            re.search(r'src="(/assets/shell/[0-9a-f]{16}\.js)"', html).group(1),
        )

    def test_both_bundles_are_served_for_a_year_under_their_hash(self) -> None:
        css_url, js_url = self._urls()
        for url, media_type, body in ((css_url, "text/css", main._home_styles()), (js_url, "text/javascript", main._home_script())):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertIn(media_type, response.headers["content-type"])
            self.assertEqual(response.headers["cache-control"], "public, max-age=31536000, immutable")
            self.assertEqual(response.text, body)
            self.assertEqual(self.client.head(url).status_code, 200)

    def test_a_stale_hash_is_not_answered_with_the_current_bytes(self) -> None:
        # An immutable URL that served different bytes would pin them in shared caches.
        self.assertEqual(self.client.get("/assets/shell/0000000000000000.css").status_code, 404)
        self.assertEqual(self.client.get("/assets/shell/0000000000000000.js").status_code, 404)

    def test_the_bundles_load_without_a_session_when_login_is_required(self) -> None:
        # The shell can reach login and error paths, and these carry no user data.
        css_url, js_url = self._urls()
        settings = replace(main.get_settings(), require_login=True)
        with patch.object(main, "get_settings", return_value=settings):
            # The control: this configuration really does turn a page away.
            self.assertEqual(self.client.get("/api/playlists").status_code, 401)
            self.assertEqual(self.client.get(css_url).status_code, 200)
            self.assertEqual(self.client.get(js_url).status_code, 200)

    def test_the_script_does_not_vary_between_requests(self) -> None:
        self.assertEqual(main._home_script(), main._home_script())

    def test_pages_link_the_bundles_rather_than_inlining_them(self) -> None:
        with offline_db():
            html = main._shell_head() + main._shell_foot()
        self.assertNotIn("<style>", html)
        self.assertNotIn("function showToast", html)
        self.assertLess(len(html), 2000)

    def test_the_shell_script_is_not_deferred(self) -> None:
        # Page scripts follow it inline and call its functions, so it has to run in place.
        with offline_db():
            self.assertNotIn("defer", main._shell_foot())

    def test_the_viewport_reaches_the_safe_areas(self) -> None:
        self.assertIn("viewport-fit=cover", main._shell_head())


class FeedbackTests(unittest.TestCase):
    def test_native_dialogs_are_gone(self) -> None:
        source = main._home_script() + (ASSETS / "api-tokens-v1.js").read_text(encoding="utf-8")
        self.assertNotRegex(source, r"\bwindow\.(confirm|alert)\(")
        self.assertNotRegex(source, r"(?<![\w.])(confirm|alert)\(")

    def test_the_old_toast_element_is_gone(self) -> None:
        with offline_db():
            self.assertNotIn("data-feedback-toast", main._shell_foot())
        self.assertNotIn("data-feedback-toast", main._home_script())
        self.assertNotIn(".feedback-toast", main._home_styles())

    def test_a_failure_stays_up_until_it_is_dismissed(self) -> None:
        self.assertNotRegex(main._home_script(), r"showToast\(['`]Could not [^'`]*['`]\)")

    def test_the_kit_runs_before_the_page_script_that_uses_it(self) -> None:
        script = main._home_script()
        self.assertLess(script.index("window.ofToast ="), script.index("function showToast("))


class DesignContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.css = main._home_styles()

    def test_dialogs_are_14px_on_a_flat_scrim(self) -> None:
        self.assertIn("border-radius: var(--r-panel)", _css_rule(self.css, ".feed-dialog"))
        self.assertIn(".feed-dialog::backdrop { background: var(--scrim); }", self.css)
        self.assertNotIn("::backdrop { background: rgba(0,0,0,.66)", self.css)
        self.assertIn("--scrim: rgba(0,0,0,.6);", self.css)

    def test_a_toggle_is_bone_when_on_and_never_a_status_colour(self) -> None:
        on = re.search(r"\):checked \{ background: ([^;]+);", self.css)
        self.assertEqual(on.group(1), "var(--bone)")
        self.assertIn("width: 34px; height: 20px;", self.css)

    def test_there_are_no_spinners_in_the_app_shell(self) -> None:
        self.assertNotIn("animation: spin", self.css)

    def test_shimmer_stops_for_reduced_motion(self) -> None:
        guard = self.css[self.css.rindex("@media (prefers-reduced-motion: reduce) {\n  .btn,"):]
        self.assertIn(".skeleton,", guard)
        self.assertIn("animation: none", guard)

    def test_the_placeholder_waits_for_a_failure(self) -> None:
        # Hidden while an <img> exists. The image removes itself when it fails.
        self.assertIn(":is(.thumb, .short-thumb):has(> img) > .thumb-fallback { display: none; }", self.css)
        self.assertIn(".avatar.has-image .avatar-initials { visibility: hidden; }", self.css)

    def test_old_button_names_remain_aliases(self) -> None:
        for name in (".pill-action", ".text-action", ".primary-action", ".subscribe-pill"):
            self.assertIn(name, self.css[self.css.index("/* ---------- buttons"):self.css.index("/* ---------- segmented")])


class HelperTests(unittest.TestCase):
    evil = '"><script>alert(1)</script>'

    def test_page_head_escapes_text_and_keeps_the_actions(self) -> None:
        html = main._page_head(self.evil, self.evil, '<a class="btn" href="/x">Go</a>')
        self.assertNotIn("<script>", html)
        self.assertIn('<div class="page-actions"><a class="btn" href="/x">Go</a></div>', html)

    def test_page_head_leaves_out_what_it_was_not_given(self) -> None:
        self.assertEqual(main._page_head("History"), '<header class="page-head"><h1>History</h1></header>')

    def test_empty_state_escapes_text(self) -> None:
        html = main._empty_state("history", self.evil, self.evil, '<a class="btn" href="/">Home</a>')
        self.assertNotIn("<script>", html)
        self.assertIn('class="empty-state-action"', html)
        self.assertNotIn("empty-state-action", main._empty_state("history", "Title", "Body"))

    def test_skeletons_are_hidden_from_assistive_tech_and_counted(self) -> None:
        self.assertEqual(main._skeleton_cards(3).count('aria-hidden="true"'), 3)
        self.assertEqual(main._skeleton_rows(2).count('aria-hidden="true"'), 2)
        self.assertEqual(main._skeleton_cards(0), "")
        self.assertEqual(main._skeleton_cards(-1), "")

    def test_skeleton_cards_are_not_mistaken_for_real_cards(self) -> None:
        self.assertNotIn("data-video-card", main._skeleton_cards(1) + main._skeleton_rows(1))

    def test_controls_escape_their_values(self) -> None:
        html = "".join((
            main._segmented(self.evil, [(self.evil, self.evil)], self.evil, label=self.evil),
            main._toggle(self.evil, self.evil, True, self.evil),
            main._select(self.evil, [(self.evil, self.evil)], self.evil, label=self.evil),
            main._range_field(self.evil, self.evil, 5, suffix=self.evil),
        ))
        self.assertNotIn("<script>", html)

    def test_toggle_is_a_real_checkbox(self) -> None:
        html = main._toggle("shorts", "Shorts", checked=True, note="Show the shelf")
        self.assertIn('class="switch" type="checkbox" role="switch" name="shorts" checked', html)
        self.assertNotIn(" checked", main._toggle("shorts", "Shorts"))

    def test_segmented_marks_the_selected_value(self) -> None:
        html = main._segmented("sort", [("new", "Newest"), ("old", "Oldest")], "old")
        self.assertIn('value="old" checked', html)
        self.assertNotIn('value="new" checked', html)

    def test_range_field_starts_with_the_value_on_show(self) -> None:
        html = main._range_field("level", "Level", 35, suffix="%")
        self.assertIn('data-suffix="%">35%</output>', html)
        self.assertIn('value="35"', html)
