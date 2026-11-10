"""Who may do what on a shared install: admin-only routes and per-account rate limits.

main.enforce_api_access asks `refusal(request, user)` once per request, after sign-in and
access checks. It answers None (go ahead) or the response that refuses the request:

- **Admin-only routes.** Anything that changes or inspects the server rather than the
  caller's own data: syncs, enrichment, discovery runs, vector rebuilds, the playback
  probes, install settings. With sign-in on, members get 403 `admin_required`. With
  sign-in off the one local user is the admin, so nothing here applies.
- **Rate limits** on the expensive routes (instance.LIMITS), per account: every profile
  of an account and every token it made share one counter. Admins are exempt, and with
  sign-in off nothing is limited. A refused request gets 429 with a sentence saying what
  ran out and when it comes back, plus Retry-After.

Counters live in Redis (ratelimit.py, process memory when Redis is down) under fixed
windows, keyed by an HMAC of the account key, never the key itself.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import re
import time
from html import escape
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse, Response

from . import instance, ratelimit
from .config import get_settings

# (method or "*", path regex). Matched against the whole path.
ADMIN_ONLY: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (method, re.compile(pattern)) for method, pattern in (
        ("*", r"/api/admin(/.*)?"),
        ("*", r"/api/status/(full|panel)"),
        ("*", r"/api/sync/(rss|runs)"),
        ("*", r"/api/discover/related"),
        ("*", r"/api/enrich/(status|channels|subscription-art|recent)"),
        ("*", r"/api/recommendations/(rebuild-vectors|evaluate)"),
        ("*", r"/api/ryd/refresh"),
        ("*", r"/api/playback/(diagnose|sources/test)"),
    )
)

# (method or "*", path regex, limit key). The first match counts.
LIMITED: tuple[tuple[str, re.Pattern[str], str], ...] = tuple(
    (method, re.compile(pattern), key) for method, pattern, key in (
        ("GET", r"/search", "search"),
        ("GET", r"/api/search(/html)?", "search"),
        ("POST", r"/api/playlists/import", "playlist_import"),
        ("POST", r"/api/remote-playlists/[^/]+/save", "playlist_import"),
        ("POST", r"/api/takeout/import", "takeout_import"),
        ("POST", r"/api/import/subscriptions\.opml", "opml_import"),
        ("POST", r"/api/download/[^/]+/jobs", "downloads"),
        ("GET", r"/api/download/(?!jobs(/|$))[^/]+", "downloads"),
        # Pages and calls that each cost one or more Invidious (or yt-dlp) requests.
        ("GET", r"/watch/[^/]+", "invidious"),
        ("GET", r"/channel/[^/]+(/posts(/[^/]+)?)?", "invidious"),
        ("GET", r"/playlist", "invidious"),
        ("GET", r"/api/remote-playlists/[^/]+/rows", "invidious"),
        ("GET", r"/api/channels/[^/]+/(videos|streams)/html", "invidious"),
        ("GET", r"/api/comments/[^/]+", "invidious"),
        ("POST", r"/api/comments/avatars", "invidious"),
        ("GET", r"/api/search/suggestions", "invidious"),
        ("GET", r"/api/videos/[^/]+/details", "invidious"),
        ("GET", r"/api/download/[^/]+/(formats|info)", "invidious"),
        ("GET", r"/api/transcript/[^/]+/fetch", "invidious"),
    )
)


def shared_install() -> bool:
    """Sign-in is on, so there are accounts to keep apart and roles to respect."""
    return get_settings().sign_in_enabled


def is_admin(user: dict[str, Any]) -> bool:
    from . import main

    return bool(main._user_is_admin(user))


def is_member(user: dict[str, Any]) -> bool:
    """A signed-in non-admin on an install with sign-in. Never true with sign-in off."""
    return shared_install() and not is_admin(user)


def account_key(user: dict[str, Any]) -> str:
    """The account a request counts against: profiles and tokens share their account's."""
    key = str(user.get("account_key") or user.get("user_key") or "local")
    return key.split(":p:", 1)[0]


def admin_only(method: str, path: str) -> bool:
    return any((rule_method in {"*", method}) and pattern.fullmatch(path) for rule_method, pattern in ADMIN_ONLY)


def limit_for(method: str, path: str) -> instance.Limit | None:
    method = "GET" if method == "HEAD" else method
    for rule_method, pattern, key in LIMITED:
        if rule_method in {"*", method} and pattern.fullmatch(path):
            return instance.LIMITS_BY_KEY[key]
    return None


def _bucket(limit: instance.Limit, account: str, now: float) -> tuple[str, int]:
    """The Redis key for this window, and the seconds until it ends."""
    settings = get_settings()
    secret = (settings.session_secret or settings.secret_key or "openfeed-unkeyed").encode("utf-8")
    who = hmac.new(secret, b"limit:" + account.encode("utf-8"), hashlib.sha256).hexdigest()[:32]
    window_index = int(now // limit.window)
    reset_in = int(limit.window - (now % limit.window)) or limit.window
    return f"of:limit:{limit.key}:{who}:{window_index}", reset_in


def check_limit(limit: instance.Limit, account: str, now: float | None = None) -> tuple[bool, int, int]:
    """Count one request. (allowed, allowed per window, seconds until the window resets)."""
    allowed_count = instance.limit_count(limit.key)
    if allowed_count <= 0:
        return True, 0, 0
    moment = time.time() if now is None else now
    key, reset_in = _bucket(limit, account, moment)
    used = ratelimit.hit(key, reset_in + 5)
    return used <= allowed_count, allowed_count, reset_in


def _wait_text(seconds: int) -> str:
    if seconds < 90:
        return "about a minute"
    minutes = math.ceil(seconds / 60)
    if minutes < 90:
        return f"{minutes} minutes"
    hours = math.ceil(minutes / 60)
    return f"about {hours} hours"


def limited_message(limit: instance.Limit, reset_in: int) -> str:
    return (f"{limit.message} This server limits each account so one person can't slow it down for everyone. "
            f"Try again in {_wait_text(reset_in)}.")


def _limited_response(request: Request, limit: instance.Limit, allowed: int, reset_in: int) -> Response:
    message = limited_message(limit, reset_in)
    headers = {"Retry-After": str(max(1, reset_in)), "Cache-Control": "no-store"}
    if request.url.path.startswith("/api/"):
        # `detail` is the sentence, because that is what the app's scripts show on an error.
        return JSONResponse(
            {"detail": message, "code": "rate_limited", "limit": limit.key, "allowed": allowed,
             "window_seconds": limit.window, "retry_after": reset_in, "message": message},
            status_code=429, headers=headers,
        )
    from . import account_pages

    body = (
        '<h1 id="auth-title">Slow down a little</h1>'
        f'<p class="auth-lede">{escape(message)}</p>'
        '<div class="acct-form-actions"><a class="btn btn-primary" href="/">Back to Home</a></div>'
    )
    response = account_pages._auth_page("Slow down a little", body, status_code=429)
    response.headers.update(headers)
    return response


def refusal(request: Request, user: dict[str, Any]) -> Response | None:
    """None, or the response that refuses this request for this user (see module doc)."""
    if not shared_install():
        return None
    method, path = request.method.upper(), request.url.path
    admin = is_admin(user)
    if admin_only(method, path) and not admin:
        return JSONResponse(
            {"detail": "admin_required", "message": "Only an admin can do this on this server."},
            status_code=403,
        )
    if admin or not user.get("authenticated"):
        return None
    limit = limit_for(method, path)
    if limit is None:
        return None
    ok, allowed, reset_in = check_limit(limit, account_key(user))
    return None if ok else _limited_response(request, limit, allowed, reset_in)


__all__ = ["ADMIN_ONLY", "LIMITED", "admin_only", "check_limit", "is_admin", "is_member", "limit_for", "refusal"]
