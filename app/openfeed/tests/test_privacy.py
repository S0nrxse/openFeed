"""The privacy rule: a viewer's browser loads nothing from anywhere but this server.

The page test renders the main page types through the real app with every database read
answered by hostile rows (thumbnails, avatars, banners and post images on Google and on an
unrelated host) and the network down, then parses the HTML and fails on any attribute or
stylesheet that would make the browser fetch something from another origin. Links the user
clicks to leave are allowed; resource loads are not.

The rest covers the pieces in privacy.py and outbound.py, the third-party player opt-in and
its per-response CSP, the access-log filter and the outbound proxy.
"""

from __future__ import annotations

import contextlib
import inspect
import logging
import re
import unittest
import urllib.parse
from html import escape
from html.parser import HTMLParser
from types import SimpleNamespace
from typing import Any, Iterator
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from openfeed import db, main, outbound, posts, privacy

OWN = "http://testserver"
EVIL = "https://tracker.example"
VIDEO = "dQw4w9WgXcQ"
CHANNEL = "UC" + "x" * 22
POST = "UgkxABCDEFGHIJKLMNOPQRSTUVWXYZabcdef"
UUID = "11111111-1111-1111-1111-111111111111"


# ---------------------------------------------------------------------------------------------
# The scanner
# ---------------------------------------------------------------------------------------------

_LOADING_LINK_RELS = {"stylesheet", "icon", "apple-touch-icon", "manifest", "preload", "prefetch",
                      "preconnect", "dns-prefetch", "modulepreload", "prerender", "mask-icon", "shortcut"}
_CSS_URL = re.compile(r"""url\(\s*['"]?([^'")\s]+)""", re.IGNORECASE)
_CSS_IMPORT = re.compile(r"""@import\s+(?:url\()?\s*['"]?([^'")\s;]+)""", re.IGNORECASE)


def _foreign(url: str) -> bool:
    """True when loading `url` from a page on OWN would contact another origin."""
    value = (url or "").strip()
    if not value or value.startswith(("data:", "blob:", "#", "about:")):
        return False
    absolute = urllib.parse.urljoin(OWN + "/page", value)
    parts = urllib.parse.urlsplit(absolute)
    return f"{parts.scheme}://{parts.netloc}" != OWN


class _ResourceScanner(HTMLParser):
    """Collects every URL that makes the browser load something, with where it was found."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.found: list[tuple[str, str]] = []
        self._in_style = False

    def _add(self, where: str, url: str) -> None:
        if _foreign(url):
            self.found.append((where, url))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name.lower(): value or "" for name, value in attrs}
        for name in ("src", "poster", "data", "background"):
            if name in values and tag not in {"a"}:
                self._add(f"<{tag} {name}>", values[name])
        for name in ("srcset", "imagesrcset"):
            for candidate in values.get(name, "").split(","):
                if candidate.strip():
                    self._add(f"<{tag} {name}>", candidate.strip().split()[0])
        if tag == "link" and set(values.get("rel", "").lower().split()) & _LOADING_LINK_RELS:
            self._add(f"<link rel={values.get('rel')}>", values.get("href", ""))
        if tag == "meta" and values.get("property", values.get("name", "")).lower() in {
            "og:image", "og:video", "og:audio", "twitter:image", "twitter:player"}:
            self._add(f"<meta {values.get('property') or values.get('name')}>", values.get("content", ""))
        if tag == "meta" and values.get("http-equiv", "").lower() == "refresh":
            target = values.get("content", "").partition("url=")[2]
            self._add("<meta refresh>", target)
        if tag == "base":
            self._add("<base href>", values.get("href", ""))
        if tag == "form":
            self._add("<form action>", values.get("action", ""))
        if "style" in values:
            for url in _CSS_URL.findall(values["style"]):
                self._add(f"<{tag} style>", url)
        if tag == "style":
            self._in_style = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "style":
            self._in_style = False

    def handle_data(self, data: str) -> None:
        if self._in_style:
            for url in _CSS_URL.findall(data) + _CSS_IMPORT.findall(data):
                self._add("<style>", url)


def foreign_resources(html: str) -> list[tuple[str, str]]:
    scanner = _ResourceScanner()
    scanner.feed(html)
    scanner.close()
    return scanner.found


def foreign_in_css(css: str) -> list[str]:
    return [url for url in _CSS_URL.findall(css) + _CSS_IMPORT.findall(css) if _foreign(url)]


# ---------------------------------------------------------------------------------------------
# A hostile, offline app
# ---------------------------------------------------------------------------------------------

def _hostile_row() -> dict[str, Any]:
    """One row that every list and every single read returns. Image fields point elsewhere."""
    return {
        "id": "1", "youtube_video_id": VIDEO, "youtube_channel_id": CHANNEL, "title": "A video",
        "name": "A list", "channel_title": "A channel", "author": "Someone", "duration_seconds": 245,
        "view_count": 1200, "video_count": 3, "subscriber_count": 5000,
        "thumbnail_url": "https://i9.ytimg.com/vi_webp/" + VIDEO + "/mqdefault.webp?sqp=tracking",
        "avatar_url": "https://yt3.ggpht.com/ytc/abc=s88-c-k",
        "channel_avatar_url": EVIL + "/avatar.png",
        "banner_url": "https://yt3.googleusercontent.com/banner=w2560",
        "cover_thumbnail_url": EVIL + "/cover.jpg", "cover_youtube_video_id": VIDEO,
        "author_thumbnail": EVIL + "/author.jpg",
        "description": "Links: https://www.youtube.com/redirect?event=video_description&q=https%3A%2F%2Fexample.org%2Fa&v=x "
                       "and https://example.org/b?utm_source=yt&id=7 and <img src=https://tracker.example/x.png>",
        "summary": "Chapters 0:00 Intro https://tracker.example/pixel.gif",
        "url": "https://www.youtube.com/watch?v=" + VIDEO,
        "member_titles": ["One", "Two"], "member_avatars": [EVIL + "/m1.png", "https://yt3.ggpht.com/m2"],
        "is_short": False, "source_kind": "subscription",
    }


def _stub(name: str):
    if name == "connect":
        def offline(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("no database in this test")
        return offline
    overrides: dict[str, Any] = {
        "list_collection_ids_for_subscriptions": {},
        "get_video_by_youtube_id": _hostile_row(),
    }
    if name in overrides:
        value = overrides[name]
        return lambda *args, **kwargs: value
    if name.startswith(("list_", "search_", "recent_")):
        return lambda *args, **kwargs: [_hostile_row()]
    if name.startswith(("is_", "has_")):
        return lambda *args, **kwargs: False
    if name.startswith("count"):
        return lambda *args, **kwargs: 0
    if name.startswith("get_"):
        return lambda *args, **kwargs: _hostile_row()
    return lambda *args, **kwargs: {}


class _NoNetwork:
    """Stands in for every httpx client: the network is down."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def __enter__(self) -> "_NoNetwork":
        return self

    def __exit__(self, *args: Any) -> bool:
        return False

    def _down(self, *args: Any, **kwargs: Any) -> Any:
        raise httpx.ConnectError("network disabled in this test")

    get = post = send = stream = request = _down

    def build_request(self, *args: Any, **kwargs: Any) -> Any:
        return object()

    def close(self) -> None:
        pass


def _hostile_post(post_id: str = POST) -> dict[str, Any]:
    return {
        "commentId": post_id, "authorId": CHANNEL, "author": "Someone",
        "authorThumbnails": [{"url": "//yt3.googleusercontent.com/author=s88", "width": 88}],
        "content": "A post with a link https://example.org/?fbclid=abc",
        "attachment": {"type": "image", "imageThumbnails": [
            {"url": "https://yt3.ggpht.com/postimage=s640", "width": 640},
        ]},
    }


def _posts_get(path: str, params: dict[str, str] | None = None) -> Any:
    if path.endswith("/community"):
        return {"comments": [_hostile_post()], "continuation": ""}
    if "/comments" in path:
        return {"comments": [], "continuation": ""}
    return _hostile_post()


@contextlib.contextmanager
def hostile_app() -> Iterator[TestClient]:
    with contextlib.ExitStack() as stack:
        for name, function in inspect.getmembers(db, inspect.isfunction):
            if function.__module__ == db.__name__ and not name.startswith("_"):
                stack.enter_context(patch.object(db, name, _stub(name)))
        stack.enter_context(patch.object(httpx, "Client", _NoNetwork))
        stack.enter_context(patch.object(httpx, "get", _NoNetwork()._down))
        stack.enter_context(patch.object(main, "_invidious_client", lambda: _NoNetwork()))
        stack.enter_context(patch.object(main, "_login_required_redirect", return_value=None))
        stack.enter_context(patch.object(main.ytdlp, "available", lambda: False))
        stack.enter_context(patch.object(posts, "_get", _posts_get))
        stack.enter_context(patch.object(main.search_extras, "list_not_interested_videos", return_value=[_hostile_row()]))
        stack.enter_context(patch.object(privacy, "get_choices", lambda user_key="local": dict(privacy.CHOICE_DEFAULTS)))
        posts.clear_cache()
        yield TestClient(main.app)
        posts.clear_cache()


# Every page type a viewer can open. Each must render and load nothing from elsewhere.
PAGES = (
    "/", "/?settings=privacy", "/subscriptions", "/shorts", "/search?q=music", "/channels",
    "/history", "/playlists", "/liked", "/downloads", "/not-interested", "/profiles",
    f"/watch/{VIDEO}", f"/watch/{VIDEO}/player", f"/channel/{CHANNEL}", f"/channel/{CHANNEL}?filter=shorts",
    f"/channel/{CHANNEL}?filter=playlists", f"/channel/{CHANNEL}?filter=live", f"/channel/{CHANNEL}/posts",
    f"/channel/{CHANNEL}/posts/{POST}", f"/playlist/{UUID}", f"/collection/{UUID}", "/docs", "/admin/status",
)


class NoForeignResourceTests(unittest.TestCase):
    def test_the_scanner_catches_what_it_is_for(self) -> None:
        planted = (
            f'<img src="{EVIL}/a.png"><img srcset="/vi/a.jpg 1x, {EVIL}/b.png 2x">'
            '<link rel="preconnect" href="https://i.ytimg.com"><link rel="stylesheet" href="//cdn.example/x.css">'
            f'<script src="{EVIL}/t.js"></script><iframe src="https://www.youtube-nocookie.com/embed/x"></iframe>'
            f'<div style="background:url({EVIL}/bg.png)"></div><style>@import "{EVIL}/i.css";</style>'
            f'<meta property="og:image" content="{EVIL}/og.png"><video poster="{EVIL}/p.jpg"></video>'
            '<a href="https://www.youtube.com/watch?v=x">a link is fine</a><img src="/vi/x/hq.jpg">'
        )
        self.assertEqual(len(foreign_resources(planted)), 10)

    def test_every_page_type_loads_only_from_this_server(self) -> None:
        with hostile_app() as client:
            for path in PAGES:
                with self.subTest(path=path):
                    response = client.get(path, follow_redirects=True, headers={"Accept": "text/html"})
                    self.assertEqual(response.status_code, 200, path)
                    self.assertEqual(urllib.parse.urlsplit(str(response.url)).netloc, "testserver", path)
                    self.assertIn("<html", response.text, path)
                    self.assertEqual(foreign_resources(response.text), [], path)

    def test_error_and_sign_in_pages_load_only_from_this_server(self) -> None:
        with hostile_app() as client:
            with patch.object(db, "get_channel_by_youtube_id", return_value=None):
                missing = client.get(f"/channel/{CHANNEL}", headers={"Accept": "text/html"})
            with patch.object(db, "get_video_by_youtube_id", return_value=None):
                missing_video = client.get("/watch/zzzzzzzzzzz", headers={"Accept": "text/html"})
        denied = main._access_denied_response({"authenticated": True, "display_name": "x"}).body.decode()
        admin = main._access_denied_response({"authenticated": True}, admin_only=True).body.decode()
        for name, html in (("channel 404", missing.text), ("video 404", missing_video.text),
                           ("access denied", denied), ("admin only", admin)):
            with self.subTest(page=name):
                self.assertIn("<html", html)
                self.assertEqual(foreign_resources(html), [])

    def test_the_hostile_rows_were_actually_drawn(self) -> None:
        # Guards the test above: it must exercise thumbnails, avatars, banners and post
        # images, not pass because nothing rendered.
        with hostile_app() as client:
            home = client.get("/").text
            channel = client.get(f"/channel/{CHANNEL}").text
            posts_page = client.get(f"/channel/{CHANNEL}/posts").text
        self.assertIn(f'src="/vi/{VIDEO}/', home)
        self.assertIn('src="/ggpht/banner=w2560"', channel)
        self.assertIn('src="/ggpht/postimage=s640"', posts_page)
        self.assertIn('src="/ggpht/author=s88"', posts_page)

    def test_the_stylesheets_and_player_chrome_load_nothing_from_elsewhere(self) -> None:
        self.assertEqual(foreign_in_css(main._home_styles()), [])
        self.assertEqual(foreign_in_css(main._player_chrome_css()), [])

    def test_no_page_carries_a_connection_hint_to_another_host(self) -> None:
        source = inspect.getsource(main)
        for hint in ('rel="preconnect"', 'rel="dns-prefetch"', "i.ytimg.com\" crossorigin", "googlevideo.com"):
            self.assertNotIn(hint, source)

    def test_links_out_carry_noreferrer_and_no_tracking(self) -> None:
        html = main._rich_text(
            "https://www.youtube.com/redirect?event=video_description&q=https%3A%2F%2Fexample.org%2Fa%3Fx%3D1&v=abc "
            "https://example.org/b?utm_source=yt&utm_medium=x&id=7 https://youtu.be/abc?si=TRACK"
        )
        self.assertIn('href="https://example.org/a?x=1"', html)
        self.assertIn('href="https://example.org/b?id=7"', html)
        self.assertIn('href="https://youtu.be/abc"', html)
        self.assertNotIn("youtube.com/redirect", html.split(">", 1)[0])
        for anchor in re.findall(r"<a [^>]*target=\"_blank\"[^>]*>", html):
            self.assertIn("noreferrer", anchor)
        for anchor in re.findall(r"<a [^>]*target=\"_blank\"[^>]*>", inspect.getsource(main)):
            self.assertIn("noreferrer", anchor, anchor)


class SameOriginImageTests(unittest.TestCase):
    def test_google_hosts_become_the_local_proxies(self) -> None:
        cases = {
            f"https://i.ytimg.com/vi/{VIDEO}/hqdefault.jpg": f"/vi/{VIDEO}/hqdefault.jpg",
            f"https://i9.ytimg.com/vi_webp/{VIDEO}/mqdefault.webp?sqp=x": f"/vi/{VIDEO}/mqdefault.jpg",
            "https://yt3.ggpht.com/ytc/abc=s88-c-k": "/ggpht/ytc/abc=s88-c-k",
            "//yt3.googleusercontent.com/banner=w2560": "/ggpht/banner=w2560",
            "/ggpht/already": "/ggpht/already",
            "data:image/png;base64,AAAA": "data:image/png;base64,AAAA",
        }
        for given, expected in cases.items():
            with self.subTest(given=given):
                self.assertEqual(privacy.same_origin_image(given), expected)

    def test_anything_else_is_dropped(self) -> None:
        for given in (f"{EVIL}/a.png", "//cdn.example/x.png", "javascript:alert(1)", "https://i.ytimg.com/an/odd",
                      "data:image/svg+xml,<svg onload=x>", "/\\evil.example/x", "ftp://x/y.png", None, ""):
            with self.subTest(given=given):
                self.assertEqual(privacy.same_origin_image(given), "")

    def test_the_deployments_own_invidious_keeps_only_proxied_paths(self) -> None:
        settings = SimpleNamespace(invidious_base_url="http://invidious:3000", invidious_public_url="https://inv.example")
        with patch.object(privacy, "get_settings", return_value=settings):
            self.assertEqual(privacy.same_origin_image(f"https://inv.example/vi/{VIDEO}/hq.jpg"), f"/vi/{VIDEO}/hq.jpg")
            self.assertEqual(privacy.same_origin_image("https://inv.example/feed/popular"), "")

    def test_avatars_banners_and_post_images_go_through_it(self) -> None:
        self.assertNotIn(EVIL, main._avatar_html("A", EVIL + "/a.png"))
        self.assertIn('src="/ggpht/x"', main._avatar_html("A", "https://yt3.ggpht.com/x"))
        self.assertEqual(main._proxied_avatar(EVIL + "/heart.png"), "")
        html = main._post_attachment_html({"type": "image", "images": [EVIL + "/1.png", "https://yt3.ggpht.com/2"]}, "p", None)
        self.assertNotIn(EVIL, html)
        self.assertIn('src="/ggpht/2"', html)


class HeaderTests(unittest.TestCase):
    def _headers(self, path: str, frame_origins: list[str] | None = None) -> dict[str, str]:
        headers: dict[str, str] = {}
        privacy.apply_headers(headers, path, frame_origins or [])
        return headers

    def test_every_page_gets_the_full_set(self) -> None:
        headers = self._headers("/watch/abc")
        csp = headers["Content-Security-Policy"]
        for directive in ("img-src 'self' data: blob:", "media-src 'self' blob:", "connect-src 'self'",
                          "font-src 'self'", "frame-src 'self';", "script-src 'self' 'unsafe-inline'",
                          "object-src 'none'", "base-uri 'none'", "form-action 'self'", "frame-ancestors 'self'"):
            self.assertIn(directive, csp)
        self.assertNotIn("*", csp)
        self.assertNotIn("http", csp)
        self.assertEqual(headers["Referrer-Policy"], "same-origin")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        for feature in ("browsing-topics=()", "interest-cohort=()", "camera=()", "geolocation=()", "fullscreen=(self)"):
            self.assertIn(feature, headers["Permissions-Policy"])

    def test_a_framed_player_widens_frame_src_by_exactly_its_origin(self) -> None:
        headers = self._headers("/watch/abc/player", ["https://www.youtube-nocookie.com"])
        self.assertIn("frame-src 'self' https://www.youtube-nocookie.com;", headers["Content-Security-Policy"])
        self.assertIn('fullscreen=(self "https://www.youtube-nocookie.com")', headers["Permissions-Policy"])
        # Nothing else widens with it.
        self.assertIn("img-src 'self' data: blob:;", headers["Content-Security-Policy"])

    def test_a_malformed_origin_cannot_inject_a_directive(self) -> None:
        csp = privacy.content_security_policy(["https://a.example; script-src *", "javascript:x", "https://u:p@b.example"])
        self.assertIn("frame-src 'self';", csp)
        self.assertNotIn("script-src *", csp)

    def test_the_middleware_sets_them_on_real_responses(self) -> None:
        with hostile_app() as client:
            page = client.get("/")
            api = client.get("/api/privacy")
            docs = client.get("/docs")
        self.assertIn("frame-src 'self';", page.headers["content-security-policy"])
        self.assertEqual(page.headers["referrer-policy"], "same-origin")
        self.assertIn("interest-cohort=()", page.headers["permissions-policy"])
        self.assertTrue(api.headers["content-security-policy"].startswith("default-src 'none'"))
        self.assertIn("frame-src 'self';", docs.headers["content-security-policy"])


def _player(choices: dict[str, bool], youtube_allowed: bool, sources: list[dict[str, str]] | None = None):
    settings = SimpleNamespace(youtube_fallback_enabled=youtube_allowed, invidious_fallback_list=[],
                               invidious_public_url="", invidious_base_url="http://invidious")
    with hostile_app() as client, \
         patch.object(privacy, "get_choices", lambda user_key="local": dict(choices)), \
         patch.object(privacy, "get_settings", return_value=settings), \
         patch.object(main, "_playback_sources", return_value=(sources if sources is not None else [
             {"id": "invidious", "label": "Invidious (local)", "kind": "invidious-local", "url": f"/embed/{VIDEO}", "same_origin": "1"}], "")), \
         patch.object(main, "_diagnose_playback", return_value={}):
        return client.get(f"/watch/{VIDEO}/player")


class ThirdPartyPlayerTests(unittest.TestCase):
    YT = "https://www.youtube-nocookie.com"

    def test_off_by_default(self) -> None:
        with patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("OPENFEED_PLAYBACK_FALLBACK", None)
            os.environ.pop("OPENFEED_ALLOW_PUBLIC_INSTANCES", None)
            os.environ.pop("OPENFEED_INVIDIOUS_FALLBACKS", None)
            self.assertFalse(main.get_settings().youtube_fallback_enabled)
            self.assertFalse(privacy.youtube_allowed())
            self.assertFalse(privacy.public_instances_allowed())
        self.assertEqual(privacy.normalize_choices(None), {"youtube_player": False, "public_instances": False})

    def test_not_opted_in_means_the_policy_refuses_the_frame(self) -> None:
        response = _player({"youtube_player": False, "public_instances": False}, youtube_allowed=True)
        self.assertIn("frame-src 'self';", response.headers["content-security-policy"])
        self.assertNotIn("youtube", response.headers["content-security-policy"])
        self.assertNotIn("data-on-request=", response.text)

    def test_allowed_but_not_by_the_admin_is_still_off(self) -> None:
        response = _player({"youtube_player": True, "public_instances": False}, youtube_allowed=False)
        self.assertNotIn("youtube", response.headers["content-security-policy"])
        self.assertNotIn("data-on-request=", response.text)

    def test_opted_in_allows_exactly_that_origin_and_only_as_a_button(self) -> None:
        response = _player({"youtube_player": True, "public_instances": False}, youtube_allowed=True)
        csp = response.headers["content-security-policy"]
        self.assertIn(f"frame-src 'self' {self.YT};", csp)
        self.assertEqual(csp.count("youtube"), 1)
        self.assertIn("data-on-request=", response.text)
        self.assertIn("Plays from YouTube: Google sees your IP address and what you watch.", response.text)
        # The automatic ladder holds only the private source.
        stage = re.search(r'data-sources="([^"]*)"', response.text).group(1)
        self.assertNotIn("youtube", stage)

    def test_the_player_script_never_walks_into_a_third_party_source(self) -> None:
        script = main._player_script()
        self.assertIn("isOnRequest(sources[index + 1])) { showFailure(); return; }", script)
        self.assertIn("nextPrivate(index)", script)
        self.assertIn("Play from YouTube", script)
        # Only YouTube's embed gets a referrer, and only the origin.
        self.assertIn("source.kind === 'youtube' ? 'strict-origin' : 'no-referrer'", script)

    def test_no_preconnect_in_the_player_shell(self) -> None:
        response = _player({"youtube_player": True, "public_instances": False}, youtube_allowed=True)
        self.assertNotIn("preconnect", response.text)
        self.assertNotIn("dns-prefetch", response.text)


class PrivacyApiTests(unittest.TestCase):
    def test_turning_on_a_player_the_admin_has_not_allowed_is_refused(self) -> None:
        with hostile_app() as client, \
             patch.object(privacy, "youtube_allowed", return_value=False), \
             patch.object(privacy, "set_choices") as saved:
            response = client.post("/api/privacy", json={"youtube_player": True})
        self.assertEqual(response.status_code, 403)
        saved.assert_not_called()

    def test_turning_one_off_always_works(self) -> None:
        with hostile_app() as client, \
             patch.object(privacy, "youtube_allowed", return_value=False), \
             patch.object(privacy, "set_choices", return_value={"youtube_player": False, "public_instances": False}) as saved:
            response = client.post("/api/privacy", json={"youtube_player": False})
        self.assertEqual(response.status_code, 200)
        saved.assert_called_once_with("local", youtube_player=False, public_instances=None)

    def test_an_allowed_opt_in_is_saved_for_this_user_only(self) -> None:
        with hostile_app() as client, \
             patch.object(main, "_user_key", return_value="user-a"), \
             patch.object(privacy, "youtube_allowed", return_value=True), \
             patch.object(privacy, "set_choices", return_value={"youtube_player": True, "public_instances": False}) as saved:
            response = client.post("/api/privacy", json={"youtube_player": True})
        self.assertEqual(response.status_code, 200)
        saved.assert_called_once_with("user-a", youtube_player=True, public_instances=None)

    def test_the_status_lists_every_service_and_what_it_sees(self) -> None:
        with hostile_app() as client:
            body = client.get("/api/privacy").json()
        keys = [service["key"] for service in body["services"]]
        self.assertEqual(keys, ["youtube_player", "public_instances", "sponsorblock_enabled", "dearrow_enabled", "ryd_enabled"])
        for service in body["services"]:
            self.assertTrue(service["sees"])
            self.assertIn(service["where"], {"browser", "server"})

    def test_reading_choices_fails_closed(self) -> None:
        with patch.object(db, "connect", side_effect=RuntimeError("down")):
            self.assertEqual(privacy.get_choices("x"), {"youtube_player": False, "public_instances": False})

    def test_the_privacy_pane_names_the_warning_next_to_the_switch(self) -> None:
        settings = SimpleNamespace(youtube_fallback_enabled=True, invidious_fallback_list=["https://yewtu.be"],
                                   sponsorblock_enabled=True, dearrow_enabled=True, ryd_enabled=True,
                                   invidious_base_url="", invidious_public_url="", source_url="https://example.org/src")
        with patch.object(privacy, "get_settings", return_value=settings), \
             patch.object(main, "get_settings", return_value=settings), \
             patch.object(privacy, "get_choices", lambda user_key="local": {"youtube_player": True, "public_instances": False}), \
             patch.object(db, "get_feed_preferences", return_value={"ryd_enabled": True}), \
             patch.object(db, "get_playback_preferences", return_value={"public_invidious_urls": []}):
            html = main._privacy_pane("local")
        self.assertIn(escape(privacy.YOUTUBE_WARNING), html)
        self.assertIn(escape(privacy.PUBLIC_INSTANCE_WARNING), html)
        self.assertRegex(html, r'data-privacy-choice="youtube_player"[^>]* checked')
        self.assertRegex(html, r'data-privacy-feed="ryd_enabled" data-privacy-mirror="\[data-setting-ryd\]" checked')
        self.assertIn("On for you: YouTube player, Return YouTube Dislike.", html)
        self.assertIn('rel="noreferrer noopener"', html)


class LogTests(unittest.TestCase):
    def _record(self) -> logging.LogRecord:
        return logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                                 ("203.0.113.9:5555", "GET", "/search?q=something+private", "1.1", 200), None)

    def test_addresses_and_query_strings_stay_out_of_the_access_log(self) -> None:
        with patch.dict("os.environ", {"OPENFEED_LOG_CLIENT_DETAILS": ""}):
            record = self._record()
            privacy.AccessLogFilter().filter(record)
        line = record.getMessage()
        self.assertNotIn("203.0.113.9", line)
        self.assertNotIn("private", line)
        self.assertIn("GET /search", line)

    def test_the_debug_switch_keeps_them(self) -> None:
        with patch.dict("os.environ", {"OPENFEED_LOG_CLIENT_DETAILS": "true"}):
            record = self._record()
            privacy.AccessLogFilter().filter(record)
            self.assertIn("203.0.113.9", record.getMessage())
            self.assertEqual(privacy.loggable("cats"), "'cats'")
        self.assertEqual(privacy.loggable("cats"), "<4 chars>")

    def test_the_filter_is_installed_on_the_access_logger(self) -> None:
        self.assertTrue(any(isinstance(item, privacy.AccessLogFilter) for item in logging.getLogger("uvicorn.access").filters))


class OutboundProxyTests(unittest.TestCase):
    def test_unset_means_direct(self) -> None:
        with patch.dict("os.environ", {"OPENFEED_OUTBOUND_PROXY": ""}):
            self.assertEqual(outbound.proxy_url(), "")
            self.assertEqual(outbound.ytdlp_args(), [])

    def test_a_proxy_reaches_httpx_and_yt_dlp(self) -> None:
        seen: dict[str, Any] = {}

        class _Client:
            def __init__(self, **kwargs: Any) -> None:
                seen.update(kwargs)

        with patch.dict("os.environ", {"OPENFEED_OUTBOUND_PROXY": "socks5h://user:secret@127.0.0.1:9050"}), \
             patch.object(httpx, "Client", _Client):
            outbound.client(timeout=3)
            self.assertEqual(outbound.ytdlp_args(), ["--proxy", "socks5h://user:secret@127.0.0.1:9050"])
            self.assertIn("--proxy", main.ytdlp.base_args())
            self.assertEqual(outbound.proxy_label(), "socks5h://127.0.0.1:9050")
        self.assertEqual(seen["proxy"], "socks5h://user:secret@127.0.0.1:9050")
        self.assertEqual(seen["headers"]["user-agent"], outbound.USER_AGENT)

    def test_a_bad_value_is_reported_not_half_applied(self) -> None:
        with patch.dict("os.environ", {"OPENFEED_OUTBOUND_PROXY": "tor://127.0.0.1"}):
            self.assertEqual(outbound.proxy_url(), "")
            self.assertTrue(outbound.proxy_problem())

    def test_third_party_lookups_use_the_outbound_client(self) -> None:
        from openfeed import captions, dearrow, enrichment, rss, ryd, sponsorblock

        for module in (sponsorblock, dearrow, ryd, rss, enrichment, captions):
            with self.subTest(module=module.__name__):
                self.assertIn("outbound.client(", inspect.getsource(module))

    def test_sponsorblock_and_dearrow_send_only_a_hash_prefix(self) -> None:
        from openfeed import dearrow, sponsorblock

        self.assertIn("{SPONSORBLOCK_ENDPOINT}/{hash_prefix(video_id)}", inspect.getsource(sponsorblock))
        self.assertIn("{DEARROW_ENDPOINT}/{prefix}", inspect.getsource(dearrow))
        self.assertEqual(len(sponsorblock.hash_prefix(VIDEO)), 4)
        self.assertEqual(len(dearrow.hash_prefix(VIDEO)), 4)


class NoPhoneHomeTests(unittest.TestCase):
    def test_the_viewer_never_gets_a_third_party_host_in_a_resource_from_code(self) -> None:
        # No analytics, gravatar, fonts or CDN anywhere in the package.
        import pathlib

        root = pathlib.Path(main.__file__).parent
        banned = ("gravatar", "googletagmanager", "google-analytics", "fonts.googleapis", "fonts.gstatic",
                  "cdn.jsdelivr", "unpkg.com", "cdnjs", "sentry.io", "plausible.io", "posthog", "segment.io")
        for path in list(root.glob("*.py")) + list(root.glob("assets/**/*.js")) + list(root.glob("assets/**/*.css")):
            text = path.read_text(errors="replace").lower()
            for word in banned:
                if word == "cdn.jsdelivr" and path.name == "apidocs.py":
                    continue  # named only to explain why it is not used
                with self.subTest(path=path.name, word=word):
                    self.assertNotIn(word, text)


if __name__ == "__main__":
    unittest.main()
