"""Load every kind of OpenFeed page in a real browser and list each request that leaves the origin.

OpenFeed's rule: the viewer's browser never contacts Google or any other third party unless
that viewer turned on a feature that says so. This script checks that rule against a running
server. It opens each page type on a desktop and a phone viewport, records every request the
browser makes (the page, its iframes, and its workers), and reports any request whose origin
is not the server's own. It also records Content-Security-Policy violations, because a request
the policy blocked is still something the page tried to load.

Run it before a release, against a server with typical settings (no third-party features on):

    python -m openfeed.tools.privacy_audit --base http://127.0.0.1:8060 --out /tmp/privacy-audit

or, without installing the package, `python app/openfeed/tools/privacy_audit.py ...`.

Needs Playwright for Python and a Chromium (`pip install playwright && playwright install
chromium`, or pass --chrome with the path to an existing Chrome binary). The server is only
read from: the script never submits a form or changes a setting.

Options:
  --base URL          the server to audit (required)
  --out DIR           where report.json, report.txt and screenshots go
  --chrome PATH       a Chromium/Chrome binary to use instead of Playwright's own
  --bypass-csp        also run every page with the server's CSP switched off, which shows what
                      the markup would load if the policy were not there (recommended)
  --only NAME,...     audit only these page names (see PAGES below)
  --viewports LIST    desktop, phone or both (default desktop,phone)
  --csp-only-bypassed run only the CSP-off pass (a quick look at what the markup loads)
  --settle SECONDS    how long to let each page load and run before reading (default 7)
  --screenshots       save a screenshot of each page and viewport
  --cookie NAME=VALUE send this cookie with every page (repeatable), to audit signed in:
                      for example the session cookie, openfeed_session=v2....

Exit status is 0 when nothing left the origin and no CSP violation was seen, 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

VIEWPORTS = {
    "desktop": {"viewport": {"width": 1440, "height": 900}},
    "phone": {"viewport": {"width": 390, "height": 844}, "is_mobile": True, "has_touch": True, "device_scale_factor": 2},
}

# Sent to every frame and worker-owning document: CSP violations become console lines the
# script can collect from any frame, including the player iframes.
_VIOLATION_HOOK = """
document.addEventListener('securitypolicyviolation', (event) => {
  try {
    console.warn('OPENFEED-CSP-VIOLATION ' + JSON.stringify({
      directive: event.effectiveDirective || event.violatedDirective,
      blocked: event.blockedURI, page: location.href,
    }));
  } catch (error) {}
});
"""

_LOCAL_SCHEMES = ("data:", "about:", "chrome-extension:", "chrome:", "devtools:")


_COOKIES: list[str] = []


def _get(base: str, path: str) -> str:
    headers = {"user-agent": "openfeed-privacy-audit"}
    if _COOKIES:
        headers["cookie"] = "; ".join(_COOKIES)
    request = urllib.request.Request(urllib.parse.urljoin(base, path), headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.read().decode("utf-8", "replace")
    except Exception:
        return ""


def _first(pattern: str, text: str) -> str:
    match = re.search(pattern, text)
    return match.group(1) if match else ""


def discover_pages(base: str) -> dict[str, str]:
    """Every page type, with real ids read from the server's own pages."""
    home = _get(base, "/")
    video = _first(r'href="/watch/([A-Za-z0-9_-]{11})"', home)
    channel = _first(r'href="/channel/(UC[A-Za-z0-9_-]{22})"', home)
    playlists = _get(base, "/playlists")
    playlist = _first(r'href="/playlist/([^"?/]+)"', playlists)
    collections = _get(base, "/collections") or ""
    collection = _first(r'href="/collection/([^"?/]+)"', collections)
    posts_html = _get(base, f"/channel/{channel}/posts") if channel else ""
    post = _first(r'href="/channel/UC[A-Za-z0-9_-]{22}/posts/([^"?/]+)"', posts_html)
    pages = {
        "home": "/",
        "subscriptions": "/subscriptions",
        "shorts": "/shorts",
        "search": "/search?q=music",
        "channels": "/channels",
        "history": "/history",
        "playlists": "/playlists",
        "liked": "/liked",
        "downloads": "/downloads",
        "not-interested": "/not-interested",
        "profiles": "/profiles",
        "settings-privacy": "/?settings=privacy",
        "settings-playback": "/?settings=playback",
        "admin-status": "/admin/status",
        "sign-in": "/auth/login?next=/",
        # Sign-in, setup, Account & security and Users. Signed out, most of these answer with
        # the sign-in page; pass --cookie to see them signed in.
        "setup": "/setup",
        "sign-up": "/auth/signup",
        "two-step": "/auth/2fa",
        "confirm": "/auth/confirm?next=/account",
        "invite": "/auth/invite/not-a-real-invite",
        "reset": "/auth/reset/not-a-real-reset",
        "account": "/account",
        "account-2fa": "/account/2fa",
        "admin-users": "/admin/users",
        "error-404": "/this-page-does-not-exist",
        "error-video": "/watch/zzzzzzzzzzz",
    }
    if video:
        pages.update({
            "watch": f"/watch/{video}",
            "player": f"/watch/{video}/player",
            "embed": f"/embed/{video}",
        })
    if channel:
        pages.update({
            "channel": f"/channel/{channel}",
            "channel-shorts": f"/channel/{channel}?filter=shorts",
            "channel-live": f"/channel/{channel}?filter=live",
            "channel-playlists": f"/channel/{channel}?filter=playlists",
            "channel-posts": f"/channel/{channel}/posts",
        })
    if post and channel:
        pages["post"] = f"/channel/{channel}/posts/{post}"
    if playlist:
        pages["playlist"] = f"/playlist/{playlist}"
    if collection:
        pages["collection"] = f"/collection/{collection}"
    return pages


def _origin(url: str) -> str:
    # blob:http://host/uuid belongs to the origin inside it.
    if url.startswith("blob:"):
        url = url[len("blob:"):]
    parts = urllib.parse.urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower()


def audit(args: argparse.Namespace) -> dict[str, Any]:
    from playwright.sync_api import sync_playwright

    base = args.base.rstrip("/")
    own = _origin(base)
    pages = discover_pages(base)
    if args.only:
        wanted = {name.strip() for name in args.only.split(",") if name.strip()}
        pages = {name: path for name, path in pages.items() if name in wanted}
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    modes = [True] if args.csp_only_bypassed else [False, True] if args.bypass_csp else [False]
    viewports = {name: VIEWPORTS[name] for name in args.viewports.split(",") if name in VIEWPORTS}
    results: list[dict[str, Any]] = []

    with sync_playwright() as playwright:
        launch: dict[str, Any] = {"args": ["--no-sandbox"]}
        if args.chrome:
            launch["executable_path"] = args.chrome
        browser = playwright.chromium.launch(**launch)
        try:
            for bypass in modes:
                for viewport_name, viewport in viewports.items():
                    for name, path in pages.items():
                        context = browser.new_context(bypass_csp=bypass, service_workers="allow", **viewport)
                        if _COOKIES:
                            context.add_cookies([{"name": c.split("=", 1)[0], "value": c.split("=", 1)[1], "url": base} for c in _COOKIES])
                        context.add_init_script(_VIOLATION_HOOK)
                        requests: list[dict[str, str]] = []
                        violations: list[dict[str, Any]] = []

                        def on_request(request, requests=requests) -> None:
                            url = request.url
                            if url.startswith(_LOCAL_SCHEMES):
                                return
                            frame = ""
                            try:
                                frame = request.frame.url if request.frame else ""
                            except Exception:
                                frame = "worker"
                            requests.append({"url": url, "type": request.resource_type, "frame": frame})

                        context.on("request", on_request)
                        page = context.new_page()

                        def on_console(message, violations=violations) -> None:
                            text = message.text
                            if text.startswith("OPENFEED-CSP-VIOLATION "):
                                try:
                                    violations.append(json.loads(text.split(" ", 1)[1]))
                                except ValueError:
                                    violations.append({"raw": text})

                        page.on("console", on_console)
                        page.on("worker", lambda worker, requests=requests: requests.append(
                            {"url": worker.url, "type": "worker", "frame": "worker"}))
                        status = 0
                        error = ""
                        try:
                            response = page.goto(base + path, wait_until="domcontentloaded", timeout=45000)
                            status = response.status if response else 0
                            page.wait_for_timeout(int(args.settle * 1000 * 0.6))
                            # Lazy images and infinite lists load on scroll.
                            page.evaluate("window.scrollTo(0, document.body ? document.body.scrollHeight : 0)")
                            page.wait_for_timeout(int(args.settle * 1000 * 0.4))
                            if args.screenshots:
                                page.screenshot(path=str(out / f"{'nocsp-' if bypass else ''}{viewport_name}-{name}.png"))
                        except Exception as exc:  # noqa: BLE001 - a page that fails to load is reported, not fatal
                            error = f"{type(exc).__name__}: {exc}".splitlines()[0][:300]
                        foreign = sorted({item["url"] for item in requests if _origin(item["url"]) != own})
                        results.append({
                            "page": name, "path": path, "viewport": viewport_name, "csp_bypassed": bypass,
                            "status": status, "error": error, "requests": len(requests),
                            "foreign_requests": [item for item in requests if item["url"] in foreign],
                            "csp_violations": violations,
                        })
                        mark = "LEAK" if foreign or violations else "ok  "
                        print(f"{mark} {'nocsp' if bypass else 'csp  '} {viewport_name:7} {name:18} {status} "
                              f"{len(requests):4} requests, {len(foreign)} foreign, {len(violations)} violations"
                              f"{'  ' + error if error else ''}", flush=True)
                        context.close()
        finally:
            browser.close()

    summary = {
        "base": base,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "pages": pages,
        "foreign_total": sum(len(item["foreign_requests"]) for item in results),
        "violations_total": sum(len(item["csp_violations"]) for item in results),
        "results": results,
    }
    (out / "report.json").write_text(json.dumps(summary, indent=2))
    lines = [f"OpenFeed privacy audit of {base} at {summary['generated_at']}",
             f"Foreign requests: {summary['foreign_total']}   CSP violations: {summary['violations_total']}", ""]
    for item in results:
        if not item["foreign_requests"] and not item["csp_violations"]:
            continue
        lines.append(f"{item['page']} ({item['viewport']}, {'CSP off' if item['csp_bypassed'] else 'CSP on'}) {item['path']}")
        for request in item["foreign_requests"]:
            lines.append(f"  request   {request['type']:10} {request['url'][:200]}  [frame {request['frame'][:80]}]")
        for violation in item["csp_violations"]:
            lines.append(f"  violation {violation}")
    (out / "report.txt").write_text("\n".join(lines) + "\n")
    print("\n".join(lines[:2]))
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base", required=True)
    parser.add_argument("--out", default="privacy-audit")
    parser.add_argument("--chrome", default="")
    parser.add_argument("--bypass-csp", action="store_true")
    parser.add_argument("--only", default="")
    parser.add_argument("--viewports", default="desktop,phone")
    parser.add_argument("--csp-only-bypassed", action="store_true")
    parser.add_argument("--settle", type=float, default=7.0)
    parser.add_argument("--screenshots", action="store_true")
    parser.add_argument("--cookie", action="append", default=[])
    args = parser.parse_args(argv)
    _COOKIES[:] = [c for c in args.cookie if "=" in c]
    summary = audit(args)
    return 1 if summary["foreign_total"] or summary["violations_total"] else 0


if __name__ == "__main__":
    sys.exit(main())
