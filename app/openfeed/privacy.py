"""What the viewer's browser may load, and which outside services each user has let in.

OpenFeed's rule, set by the owner: the viewer's browser never contacts Google or any other
third party unless that viewer turned on a feature that says so in plain words. This module
holds the pieces that enforce it, so they live in one place:

- `same_origin_image()` turns any image URL the app is about to render into one on this
  origin (the /vi/ and /ggpht/ proxies) or drops it.
- `clean_outbound_url()` unwraps YouTube's redirect links and strips tracking parameters
  from links a user can click to leave.
- `content_security_policy()` and `permissions_policy()` build the response headers. A page
  that has to frame a third-party player names that origin per response, and only then.
- `AccessLogFilter` keeps client addresses and query strings (search terms) out of logs.
- The per-user opt-ins for third-party players: the YouTube embed and public Invidious
  instances. Each needs the admin to allow it AND the user to switch it on, and neither is
  ever used automatically: the player offers them only as a button the viewer presses.
- `services()` describes every outside service for the Settings → Privacy pane.

PRIVACY.md at the repository root is the user-facing statement of the same rules.
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.parse
from typing import Any, Iterable

from .config import get_settings

YOUTUBE_EMBED_ORIGIN = "https://www.youtube-nocookie.com"

YOUTUBE_WARNING = "Plays from YouTube: Google sees your IP address and what you watch."
PUBLIC_INSTANCE_WARNING = (
    "Plays from someone else's Invidious server: whoever runs it sees your IP address and what you watch."
)

# ---------------------------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------------------------

_YTIMG_PATH = re.compile(r"^/(?:vi|vi_webp|an_webp)/([A-Za-z0-9_-]{11})/([A-Za-z0-9_.-]+)$")
_GOOGLE_IMAGE_HOSTS = ("ggpht.com", "googleusercontent.com")
# The paths this server proxies to its own Invidious. Anything else on that host is not ours
# to rewrite.
_PROXIED_PREFIXES = ("/vi/", "/ggpht/")


def _host_matches(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def _invidious_origins() -> set[str]:
    settings = get_settings()
    origins = set()
    for base in (settings.invidious_base_url, settings.invidious_public_url):
        parts = urllib.parse.urlsplit(str(base or ""))
        if parts.scheme and parts.netloc:
            origins.add(f"{parts.scheme}://{parts.netloc}".lower())
    return origins


def same_origin_image(url: Any) -> str:
    """The same image on this origin, or "" when there is no safe way to show it.

    - a path on this origin ("/vi/...", "/ggpht/...", "/assets/...") is kept;
    - i.ytimg.com (any iN. or vi_webp form) becomes /vi/<id>/<name>.jpg;
    - yt3.ggpht.com and *.googleusercontent.com become /ggpht/<path>;
    - the deployment's own Invidious address keeps its path when it is /vi/ or /ggpht/;
    - data:image/ URIs are kept (they load nothing);
    - anything else, including protocol-relative and javascript: URLs, is dropped.

    Dropping is the safe failure: every caller already draws a placeholder for a missing
    image, and a missing picture costs nothing, while a loaded one hands the viewer's IP to
    whoever hosts it.
    """
    text = str(url or "").strip()
    if not text:
        return ""
    if text.startswith("data:image/") and not text.startswith("data:image/svg"):
        return text
    if text.startswith("/") and not text.startswith("//") and "\\" not in text:
        return text
    if text.startswith("//"):
        text = "https:" + text
    try:
        parts = urllib.parse.urlsplit(text)
    except ValueError:
        return ""
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return ""
    host = parts.hostname.lower()
    if _host_matches(host, "ytimg.com"):
        match = _YTIMG_PATH.match(parts.path)
        if not match:
            return ""
        name = match.group(2)
        stem = name.rsplit(".", 1)[0] if "." in name else name
        return f"/vi/{match.group(1)}/{stem}.jpg"
    if any(_host_matches(host, domain) for domain in _GOOGLE_IMAGE_HOSTS):
        path = parts.path.lstrip("/")
        if path.startswith("ggpht/"):
            path = path[len("ggpht/"):]
        if not path:
            return ""
        return "/ggpht/" + path + (f"?{parts.query}" if parts.query else "")
    origin = f"{parts.scheme}://{parts.netloc}".lower()
    if origin in _invidious_origins() and parts.path.startswith(_PROXIED_PREFIXES):
        return parts.path + (f"?{parts.query}" if parts.query else "")
    return ""


# ---------------------------------------------------------------------------------------------
# Links a user clicks to leave
# ---------------------------------------------------------------------------------------------

# Click-tracking parameters, removed from every outgoing link.
_TRACKING_PARAMS = {
    "fbclid", "gclid", "dclid", "gbraid", "wbraid", "msclkid", "mc_cid", "mc_eid", "igshid",
    "yclid", "twclid", "ttclid", "li_fat_id", "_hsenc", "_hsmi", "mkt_tok",
}
_TRACKING_PREFIXES = ("utm_", "pk_", "mtm_", "hsa_")
# Only tracking on YouTube's own links (share id, placement, referral feature); elsewhere a
# parameter with the same name may be the page's real content.
_YOUTUBE_TRACKING_PARAMS = {"si", "pp", "feature", "ab_channel", "embeds_referring_euri", "source_ve_path"}
_YOUTUBE_HOSTS = ("youtube.com", "youtu.be", "youtube-nocookie.com")


def clean_outbound_url(url: str) -> str:
    """A link the user may click, without YouTube's redirect wrapper or tracking parameters.

    YouTube rewrites links in descriptions to youtube.com/redirect?q=<real url>&event=...,
    which would tell Google about every click. The real address is taken out of `q`.
    Common click-tracking parameters (utm_*, fbclid, gclid, and YouTube's si and pp on its
    own links) are removed. Anything that is not http(s) comes back unchanged for the caller
    to refuse.
    """
    text = str(url or "")
    try:
        parts = urllib.parse.urlsplit(text)
    except ValueError:
        return text
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return text
    host = parts.hostname.lower()
    if _host_matches(host, "youtube.com") and parts.path == "/redirect":
        target = urllib.parse.parse_qs(parts.query).get("q", [""])[0]
        if target.lower().startswith(("http://", "https://")):
            return clean_outbound_url(target)
    youtube = any(_host_matches(host, domain) for domain in _YOUTUBE_HOSTS)

    def tracking(key: str) -> bool:
        name = key.lower()
        return name in _TRACKING_PARAMS or name.startswith(_TRACKING_PREFIXES) or (youtube and name in _YOUTUBE_TRACKING_PARAMS)

    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    kept = [(key, value) for key, value in query if not tracking(key)]
    if len(kept) == len(query):
        return text
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(kept), parts.fragment))


# ---------------------------------------------------------------------------------------------
# Response headers
# ---------------------------------------------------------------------------------------------

def _origin_of(url: str) -> str:
    try:
        parts = urllib.parse.urlsplit(str(url or "").strip())
    except ValueError:
        return ""
    if parts.scheme not in {"http", "https"} or not parts.netloc or parts.username or parts.password:
        return ""
    if any(character in parts.netloc for character in " ;,'\"*"):
        return ""
    return f"{parts.scheme}://{parts.netloc}".lower()


def content_security_policy(frame_origins: Iterable[str] = (), *, embed: bool = False) -> str:
    """The page policy: this origin only, plus exactly the third-party frames this response needs.

    'unsafe-inline' stays on scripts and styles because the app shell and the player are
    built from inline code and inline event handlers. It lets this server's own inline code
    run; it does not let anything load from elsewhere, which is what the rest of the policy
    is for. The Invidious embed (embed=True) additionally needs video.js's data: icon font
    and its blob: transmuxer worker.
    """
    frames = ["'self'"]
    for origin in frame_origins:
        clean = _origin_of(origin)
        if clean and clean not in frames:
            frames.append(clean)
    directives = [
        ("default-src", "'self'"),
        ("script-src", "'self' 'unsafe-inline'"),
        ("style-src", "'self' 'unsafe-inline'"),
        ("img-src", "'self' data: blob:"),
        ("media-src", "'self' blob:"),
        ("font-src", "'self' data:" if embed else "'self'"),
        ("connect-src", "'self'"),
        ("frame-src", " ".join(frames)),
        ("worker-src", "'self' blob:" if embed else "'self'"),
        ("manifest-src", "'self'"),
        ("object-src", "'none'"),
        ("base-uri", "'none'"),
        ("form-action", "'self'"),
        ("frame-ancestors", "'self'"),
    ]
    return "; ".join(f"{name} {value}" for name, value in directives)


API_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"

# Features no OpenFeed page uses, switched off for every document and every frame inside it,
# including the ad-tech ones (Topics, FLoC's interest-cohort, FLEDGE, attribution reporting).
# The clipboard is left at its default on purpose: the Copy buttons use it. Passkeys
# (publickey-credentials-get/-create) are granted to this origin only, never to a frame from
# elsewhere: sign-in and Account & security use them (passkeys.py).
_DISABLED_FEATURES = (
    "accelerometer", "ambient-light-sensor", "attribution-reporting", "battery", "bluetooth",
    "browsing-topics", "camera", "compute-pressure", "display-capture", "document-domain",
    "geolocation", "gyroscope", "hid", "identity-credentials-get", "idle-detection",
    "interest-cohort", "join-ad-interest-group", "local-fonts", "magnetometer", "microphone",
    "midi", "otp-credentials", "payment", "private-aggregation", "private-state-token-issuance",
    "private-state-token-redemption", "run-ad-auction", "serial", "shared-storage",
    "shared-storage-select-url", "storage-access", "usb", "window-management",
    "xr-spatial-tracking",
)
# Used by the player. Granted to this origin, and to a third-party player only on the one
# response that frames it.
_PLAYER_FEATURES = ("autoplay", "encrypted-media", "fullscreen", "picture-in-picture")
_SELF_ONLY_FEATURES = ("publickey-credentials-create", "publickey-credentials-get")


def permissions_policy(frame_origins: Iterable[str] = ()) -> str:
    extra = " ".join(f'"{origin}"' for origin in (_origin_of(item) for item in frame_origins) if origin)
    allowed = f"(self{' ' + extra if extra else ''})"
    parts = [f"{feature}=()" for feature in _DISABLED_FEATURES]
    parts += [f"{feature}={allowed}" for feature in _PLAYER_FEATURES]
    parts += [f"{feature}=(self)" for feature in _SELF_ONLY_FEATURES]
    return ", ".join(parts)


# Same-origin requests keep their Referer and, more importantly, a real Origin header on
# POSTs, which the CSRF check in main.harden_http compares against the host. ("no-referrer"
# would make the browser send "Origin: null" on every same-origin POST.) Cross-origin
# requests get nothing, so no third party ever learns which OpenFeed page a link came from.
REFERRER_POLICY = "same-origin"


def apply_headers(headers: Any, path: str, frame_origins: Iterable[str] = ()) -> None:
    """Set every privacy header on one response. `headers` is a MutableHeaders.

    Cross-Origin-Opener-Policy keeps a page another site opened (or opened from here) out of
    this one's window group; Cross-Origin-Resource-Policy stops other sites from loading this
    server's responses (thumbnails, video, JSON) as subresources. Same-origin loads, the
    /embed player iframe and Range requests for video are untouched by either."""
    headers["X-Content-Type-Options"] = "nosniff"
    headers["Referrer-Policy"] = REFERRER_POLICY
    headers["Cross-Origin-Opener-Policy"] = "same-origin"
    headers["Cross-Origin-Resource-Policy"] = "same-origin"
    if path.startswith("/api/"):
        headers["X-Frame-Options"] = "DENY"
        headers["Content-Security-Policy"] = API_CSP
        headers["Permissions-Policy"] = permissions_policy()
        return
    origins = list(frame_origins)
    headers["X-Frame-Options"] = "SAMEORIGIN"
    headers["Content-Security-Policy"] = content_security_policy(origins, embed=path.startswith("/embed/"))
    headers["Permissions-Policy"] = permissions_policy(origins)


# ---------------------------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------------------------

def log_client_details() -> bool:
    """True when OPENFEED_LOG_CLIENT_DETAILS asks for addresses and query strings in logs."""
    return (os.getenv("OPENFEED_LOG_CLIENT_DETAILS") or "").strip().lower() in {"1", "true", "yes", "on"}


class AccessLogFilter(logging.Filter):
    """Drop the client address and the query string from uvicorn's access log lines.

    uvicorn logs `'%s - "%s %s HTTP/%s" %d'` with (client_addr, method, full_path,
    http_version, status_code). The address identifies the viewer and the query string
    holds their searches, so both go unless OPENFEED_LOG_CLIENT_DETAILS is on for debugging.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if log_client_details():
            return True
        args = record.args
        if isinstance(args, tuple) and len(args) == 5:
            client, method, full_path, version, status_code = args
            path = str(full_path).split("?", 1)[0]
            record.args = ("-", method, path, version, status_code)
        return True


def loggable(text: Any) -> str:
    """What may go in an application log line about something a user typed (a search).

    The text itself only when OPENFEED_LOG_CLIENT_DETAILS is on; otherwise its length.
    """
    value = str(text or "")
    return repr(value) if log_client_details() else f"<{len(value)} chars>"


def install_log_filters() -> None:
    """Attach the access-log filter once. Safe to call more than once."""
    for name in ("uvicorn.access", "gunicorn.access"):
        logger = logging.getLogger(name)
        if not any(isinstance(item, AccessLogFilter) for item in logger.filters):
            logger.addFilter(AccessLogFilter())


# ---------------------------------------------------------------------------------------------
# Per-user choices about third-party players
# ---------------------------------------------------------------------------------------------

CHOICE_DEFAULTS: dict[str, bool] = {"youtube_player": False, "public_instances": False}


def youtube_allowed() -> bool:
    """The admin's switch: OPENFEED_PLAYBACK_FALLBACK=youtube. Off by default."""
    return bool(get_settings().youtube_fallback_enabled)


def public_instances_allowed() -> bool:
    """The admin's switch for public Invidious instances.

    On when OPENFEED_ALLOW_PUBLIC_INSTANCES is true (users may add their own) or when the
    admin listed instances in OPENFEED_INVIDIOUS_FALLBACKS (users may pick from those).
    """
    return user_instances_allowed() or bool(get_settings().invidious_fallback_list)


def user_instances_allowed() -> bool:
    """OPENFEED_ALLOW_PUBLIC_INSTANCES, or the admin's switch under Admin, Server."""
    value = getattr(get_settings(), "allow_public_instances", None)
    if value is None:  # a stand-in Settings without the field (tests)
        return (os.getenv("OPENFEED_ALLOW_PUBLIC_INSTANCES") or "").strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def normalize_choices(stored: Any) -> dict[str, bool]:
    stored = stored if isinstance(stored, dict) else {}
    return {key: bool(stored.get(key, default)) for key, default in CHOICE_DEFAULTS.items()}


def get_choices(user_key: str = "local") -> dict[str, bool]:
    """This user's opt-ins. Reading fails closed: an error means nothing is opted in."""
    from . import db

    try:
        with db.connect() as conn:
            with conn.cursor() as cur:
                user_id = db._ensure_local_user_id(cur, user_key)
                cur.execute("SELECT profile -> 'privacy' AS privacy FROM users WHERE id = %s", (user_id,))
                row = cur.fetchone() or {}
            conn.commit()
    except Exception:  # noqa: BLE001 - no answer means no opt-in
        return dict(CHOICE_DEFAULTS)
    return normalize_choices(row.get("privacy"))


def set_choices(user_key: str = "local", **changes: bool | None) -> dict[str, bool]:
    """Store opt-ins. Only keys in CHOICE_DEFAULTS are kept; None leaves a key as it is."""
    from . import db

    with db.connect() as conn:
        with conn.cursor() as cur:
            user_id = db._ensure_local_user_id(cur, user_key)
            cur.execute("SELECT profile -> 'privacy' AS privacy FROM users WHERE id = %s", (user_id,))
            current = normalize_choices((cur.fetchone() or {}).get("privacy"))
            for key, value in changes.items():
                if key in CHOICE_DEFAULTS and value is not None:
                    current[key] = bool(value)
            # jsonb_set on the one key, so a concurrent write to another part of the profile
            # (playback, feed preferences) is not overwritten with a stale copy.
            cur.execute(
                "UPDATE users SET profile = jsonb_set(COALESCE(profile, '{}'::jsonb), '{privacy}', %s::jsonb, true), "
                "updated_at = now() WHERE id = %s",
                (json.dumps(current), user_id),
            )
        conn.commit()
    return current


def youtube_enabled_for(choices: dict[str, bool]) -> bool:
    return youtube_allowed() and bool(choices.get("youtube_player"))


def public_instances_enabled_for(choices: dict[str, bool]) -> bool:
    return public_instances_allowed() and bool(choices.get("public_instances"))


def instance_origin(base: str) -> str:
    return _origin_of(base)


# ---------------------------------------------------------------------------------------------
# What Settings → Privacy shows
# ---------------------------------------------------------------------------------------------

def services(choices: dict[str, bool], feed_preferences: dict[str, Any], public_urls: list[str]) -> list[dict[str, Any]]:
    """Every outside service a user can turn on, with who sees what. Order is the page order.

    `where` is "browser" when the viewer's own browser makes the connection (their IP
    address is seen) and "server" when this server makes it on their behalf.
    """
    settings = get_settings()
    instances = list(dict.fromkeys(list(public_urls if user_instances_allowed() else []) + settings.invidious_fallback_list))
    return [
        {
            "key": "youtube_player", "name": "YouTube player", "where": "browser",
            "allowed": youtube_allowed(), "on": youtube_enabled_for(choices),
            "who": "Google (www.youtube-nocookie.com and the YouTube servers it loads)",
            "sees": "Your IP address, your browser, the video you play and this site's address.",
            "when": "Only when you press “Play from YouTube” on a video OpenFeed cannot play itself. Never automatic.",
            "warning": YOUTUBE_WARNING,
        },
        {
            "key": "public_instances", "name": "Public Invidious instances", "where": "browser",
            "allowed": public_instances_allowed(), "on": public_instances_enabled_for(choices),
            "who": ", ".join(urllib.parse.urlsplit(url).netloc or url for url in instances) or "The instances you add under Playback",
            "sees": "Your IP address, your browser and the video you play.",
            "when": "Only when you press “Play from” an instance on a video OpenFeed cannot play itself. Never automatic.",
            "warning": PUBLIC_INSTANCE_WARNING,
        },
        {
            "key": "sponsorblock_enabled", "name": "SponsorBlock", "where": "server",
            "allowed": bool(settings.sponsorblock_enabled), "on": bool(settings.sponsorblock_enabled and feed_preferences.get("sponsorblock_enabled")),
            "who": "sponsor.ajay.app",
            "sees": "This server's address and the first 4 characters of a SHA-256 hash of the video ID, shared by many videos.",
            "when": "When you open a video.",
            "warning": "",
        },
        {
            "key": "dearrow_enabled", "name": "DeArrow", "where": "server",
            "allowed": bool(settings.dearrow_enabled), "on": bool(settings.dearrow_enabled and feed_preferences.get("dearrow_enabled")),
            "who": "sponsor.ajay.app",
            "sees": "This server's address and the first 4 characters of a SHA-256 hash of each video ID on the page.",
            "when": "When a page with videos loads.",
            "warning": "",
        },
        {
            "key": "ryd_enabled", "name": "Return YouTube Dislike", "where": "server",
            "allowed": bool(settings.ryd_enabled), "on": bool(settings.ryd_enabled and feed_preferences.get("ryd_enabled")),
            "who": "returnyoutubedislikeapi.com",
            "sees": "This server's address and full video IDs, mixed into batches with other videos from the library.",
            "when": "In the background sync, and when you open a video with no estimate yet.",
            "warning": "Return YouTube Dislike learns which videos this server looks up.",
        },
    ]
