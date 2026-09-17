from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import uuid
import secrets
import threading
import time
import urllib.parse
import zlib
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import format_datetime
from html import escape, unescape
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
from fastapi import FastAPI, File, HTTPException, Request, UploadFile, status
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
import redis

from . import __version__, db
from .config import get_settings
from .discovery import discover_related_videos
from .enrichment import (
    backfill_subscription_art,
    enrich_channel,
    enrich_channels,
    enrich_channels_paced,
    enrich_recent_videos,
    enrich_video,
)
from . import discovery, downloads, ryd, takeout, tokens, ytdlp
from .playback import PlaybackError, download_options, playback_for_video, select_preview_stream
from .rss import fetch_channel_feed, parse_channel_feed, sync_rss_once
from .youtube_ids import CHANNEL_ID_STRICT_RE, VIDEO_ID_RE, youtube_channel_id_from_text


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    # Subscribed channels with no avatar render as blank circles in the sidebar, and the
    # worker's slow top-up takes most of a day to reach them. Kicked here, on a thread, so
    # a restart is enough to fix a list that came in from an import.
    try:
        if db.count_subscription_channels_missing_media():
            threading.Thread(target=backfill_subscription_art, name="subscription-art", daemon=True).start()
    except Exception as exc:  # noqa: BLE001 - never block startup on cosmetics
        logger.info("subscription art backfill not started: %s", exc)
    yield


API_DESCRIPTION = """
The full OpenFeed API. Everything the web interface can do is reachable here.

## Authenticating

Browser sessions work as you would expect. For anything else — a script, a cron job,
another service — create a bearer token in Settings or via `POST /api/tokens`, then send
it as `Authorization: Bearer ofd_...`. The plaintext is shown once and stored only as a
hash, so it cannot be recovered afterwards; revoke and mint a new one instead.

A token may be marked **read-only**, in which case it is refused any method other than
GET/HEAD/OPTIONS. Token management itself always requires a signed-in session, so a
leaked token cannot renew itself.

Valid tokens delegate app access from their authorized browser issuer, even when an
access group is configured. They act as the issuer's `user_key`, not a caller-supplied
identity. New tokens snapshot the issuer's admin capability on the server; request
headers and JSON cannot supply that claim. Existing tokens remain non-admin. Only
admin-delegated tokens can read `/api/status/full` and `/api/sync/runs`; read-only
restrictions still apply, and even admin tokens cannot list, mint, or revoke tokens.
`GET /api/` reports `is_admin` and `read_only` for the current credential. Delegation
lasts until revocation; changes to the issuer's groups do not update existing tokens.
Invalid or revoked bearer credentials return 401 on the API and `/healthz`, even if
a browser session is also present. Anonymous `/healthz` checks remain public.

## Conventions

* Mutations answer `{"ok": true, ...}`; collections answer `{"items": [...]}`.
* Errors use the matching HTTP status with a `detail` string.
* `GET /api/` lists the capability groups; `/docs` and `/openapi.json` describe every route.
"""

API_TAGS = [
    {"name": "feed", "description": "Home feed, recommendations, shorts and search."},
    {"name": "videos", "description": "Individual videos, transcripts and metadata."},
    {"name": "playback", "description": "Playback sources, progress and diagnostics."},
    {"name": "downloads", "description": "Download jobs and the on-server download library."},
    {"name": "subscriptions", "description": "Channels, subscriptions and collections."},
    {"name": "playlists", "description": "Playlists and their contents."},
    {"name": "preferences", "description": "Feed and playback preferences."},
    {"name": "metadata", "description": "Enrichment, discovery and dislike estimates."},
    {"name": "tokens", "description": "API tokens. Session-only, by design."},
    {"name": "system", "description": "Status, sync and health."},
]

app = FastAPI(
    title="OpenFeed",
    version=__version__,
    description=API_DESCRIPTION,
    openapi_tags=API_TAGS,
    lifespan=lifespan,
)

logger = logging.getLogger("openfeed.web")

TAG_RE = re.compile(r"<[^>]+>")

AUTH_SESSION_TTL_SECONDS = 7 * 24 * 60 * 60
AUTH_STATE_TTL_SECONDS = 10 * 60


def _utc_timestamp() -> int:
    return int(time.time())


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))


def _auth_secret() -> bytes:
    secret = get_settings().session_secret or get_settings().secret_key
    return secret.encode("utf-8")


def _sign_payload(payload: dict[str, Any]) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    encoded = _b64url(body)
    signature = hmac.new(_auth_secret(), encoded.encode("ascii"), hashlib.sha256).digest()
    return f"{encoded}.{_b64url(signature)}"


def _unsign_payload(value: str | None) -> dict[str, Any] | None:
    if not value or "." not in value or not _auth_secret():
        return None
    encoded, signature = value.rsplit(".", 1)
    expected = _b64url(hmac.new(_auth_secret(), encoded.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        payload = json.loads(_b64url_decode(encoded).decode("utf-8"))
    except (ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _cookie_secure() -> bool:
    return get_settings().public_url.startswith("https://")


def _safe_next_path(next_path: str | None, fallback: str = "/") -> str:
    parsed = urllib.parse.urlsplit(next_path or fallback)
    if parsed.scheme or parsed.netloc or not parsed.path.startswith("/"):
        return fallback
    return urllib.parse.urlunsplit(("", "", parsed.path, parsed.query, parsed.fragment))


def _request_host(request: Request) -> str:
    forwarded_host = str(request.headers.get("x-forwarded-host") or "").split(",", 1)[0].strip()
    return (forwarded_host or str(request.headers.get("host") or "")).lower()


def _canonical_login_redirect(request: Request, next_path: str | None) -> RedirectResponse | None:
    settings = get_settings()
    if not settings.public_url:
        return None
    public = urllib.parse.urlsplit(settings.public_url)
    public_host = str(public.netloc or "").lower()
    if not public.scheme or not public_host:
        return None
    if _request_host(request) == public_host:
        return None
    target = f"{settings.public_url}/auth/login?next={urllib.parse.quote(_safe_next_path(next_path))}"
    return RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)


def _auth_state_from_request(request: Request) -> dict[str, Any] | None:
    state = _unsign_payload(request.cookies.get(get_settings().oidc_state_cookie))
    if not state or int(state.get("expires_at", 0)) <= _utc_timestamp():
        return None
    return state


def _session_from_request(request: Request) -> dict[str, Any] | None:
    session = _unsign_payload(request.cookies.get(get_settings().session_cookie))
    if not session or int(session.get("expires_at", 0)) <= _utc_timestamp():
        return None
    if not str(session.get("sub") or "").strip():
        return None
    return session


def _user_from_token(request: Request | None) -> dict[str, Any] | None:
    """Resolve an Authorization bearer token to a user, or None if there isn't a valid one.

    Checked before the session cookie so a script can authenticate without one. A revoked
    or unknown token falls through to the normal session path for browser pages. The API
    middleware refuses invalid presented credentials, including on public health checks.
    """
    if request is None:
        return None
    presented = tokens.bearer_from_header(request.headers.get("authorization"))
    prefix = tokens.split(presented)
    if not prefix:
        return None
    try:
        record = db.find_api_token(prefix)
    except Exception:
        return None
    if not record or record.get("revoked_at") or not tokens.matches(presented, record.get("token_hash", "")):
        return None
    try:
        db.touch_api_token(record["id"])
    except Exception:
        pass
    user_key = str(record.get("user_key") or "local")
    return {
        "authenticated": True,
        "user_key": user_key,
        "sub": user_key,
        "email": "",
        "display_name": f"API token: {record.get('name') or 'unnamed'}",
        "username": "api-token",
        "groups": [],
        "via_token": True,
        "token_read_only": bool(record.get("read_only")),
        "token_delegated_admin": record.get("delegated_admin") is True,
    }


def _current_user(request: Request | None) -> dict[str, Any]:
    token_user = _user_from_token(request)
    if token_user:
        return token_user
    session = _session_from_request(request) if request is not None else None
    if session:
        email = str(session.get("email") or "").strip().lower()
        name = str(session.get("name") or session.get("username") or email or "OpenFeed user").strip()
        sub = str(session.get("sub") or "").strip()
        return {"authenticated": True, "user_key": sub, "sub": sub, "email": email, "display_name": name, "username": str(session.get("username") or ""), "groups": session.get("groups") if isinstance(session.get("groups"), list) else []}
    return {"authenticated": False, "user_key": "local", "sub": "local", "email": "", "display_name": "Local OpenFeed", "username": "local", "groups": []}


def _auth_groups(user: dict[str, Any]) -> set[str]:
    groups = user.get("groups") if isinstance(user.get("groups"), list) else []
    return {str(group) for group in groups if str(group).strip()}


def _user_has_required_access(user: dict[str, Any]) -> bool:
    # A verified token delegates its authorized issuer's app access, not IdP groups.
    # This also keeps tokens issued before delegation metadata was added usable.
    if user.get("authenticated") and user.get("via_token"):
        return True
    settings = get_settings()
    required_group = settings.required_group.strip()
    if not required_group:
        return True
    if not user.get("authenticated"):
        return not settings.require_login
    groups = _auth_groups(user)
    return required_group in groups or bool(groups & settings.admin_groups)


def _access_denied_response() -> Response:
    required_group = get_settings().required_group or "the configured OpenFeed access group"
    return _auth_error_page(
        "OpenFeed access denied",
        f"Your account is missing {required_group}. Ask your identity-provider administrator to grant the matching group.",
        status_code=status.HTTP_403_FORBIDDEN,
    )


def _user_is_admin(user: dict[str, Any]) -> bool:
    """Browser admin gating needs a login and a group; tokens use persisted delegation.

    With either missing there is no identity to test, so browsers count as admin -
    that keeps single-user installs working instead of locking the owner out of their
    own status page.
    """
    # Tokens never inherit the single-user fallback or fabricated groups. Only the
    # server-derived capability saved at mint time can delegate administrator access.
    if user.get("via_token"):
        return bool(user.get("authenticated") and user.get("token_delegated_admin") is True)
    settings = get_settings()
    if not settings.require_login or not settings.admin_groups:
        return True
    return bool(_auth_groups(user) & settings.admin_groups)


@app.middleware("http")
async def enforce_api_access(request: Request, call_next):
    """Apply the same access rules to /api and /admin that the HTML pages already get.

    Page handlers call _login_required_redirect or _user_key, but most JSON endpoints
    took no request at all, so a configured required_group protected the pages while
    leaving the API - including the subscribe and unsubscribe routes - open to anyone
    who could reach the port. Enforcing it here means a new route cannot forget to.

    Static assets and the /auth handshake sit outside these prefixes. Anonymous health
    checks remain public, but explicitly presented invalid credentials fail closed.
    The Invidious proxies under /api/v1 keep their own guard as well.
    """
    path = request.url.path
    if path != "/healthz" and not path.startswith(("/api/", "/admin/")):
        return await call_next(request)

    settings = get_settings()
    user = _current_user(request)
    wants_json = path.startswith("/api/")

    if tokens.bearer_from_header(request.headers.get("authorization")) and not user.get("via_token"):
        return JSONResponse({"detail": "invalid_token"}, status_code=status.HTTP_401_UNAUTHORIZED)
    if path == "/healthz":
        return await call_next(request)

    if settings.require_login and not user.get("authenticated"):
        if wants_json:
            return JSONResponse({"detail": "login_required"}, status_code=status.HTTP_401_UNAUTHORIZED)
        return _login_required_redirect(request) or _access_denied_response()
    if user.get("authenticated") and not _user_has_required_access(user):
        if wants_json:
            return JSONResponse({"detail": "openfeed_access_denied"}, status_code=status.HTTP_403_FORBIDDEN)
        return _access_denied_response()

    # A read-only token is enforced here rather than per-route, so a route added later
    # cannot quietly become writable to a token that was never meant to change anything.
    if user.get("token_read_only") and request.method not in {"GET", "HEAD", "OPTIONS"}:
        return JSONResponse(
            {"detail": "read_only_token", "message": "This API token may only perform read requests."},
            status_code=status.HTTP_403_FORBIDDEN,
        )
    # Minting or revoking credentials with a credential would let a leaked token renew
    # itself indefinitely. Token management stays a signed-in-browser operation.
    if user.get("via_token") and path.startswith("/api/tokens"):
        return JSONResponse(
            {"detail": "session_required", "message": "API tokens can only be managed from a signed-in session."},
            status_code=status.HTTP_403_FORBIDDEN,
        )
    if path.startswith("/api/tokens") and not user.get("authenticated"):
        return JSONResponse({"detail": "session_required"}, status_code=status.HTTP_401_UNAUTHORIZED)
    if path.startswith("/admin/") and not _user_is_admin(user):
        return _access_denied_response()
    if path in {"/api/status/full", "/api/sync/runs"} and not _user_is_admin(user):
        return JSONResponse({"detail": "admin_required"}, status_code=status.HTTP_403_FORBIDDEN)

    return await call_next(request)


def _user_key(request: Request | None) -> str:
    user = _current_user(request)
    if get_settings().require_login and not user.get("authenticated"):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="login_required")
    if user.get("authenticated") and not _user_has_required_access(user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="openfeed_access_denied")
    return str(user.get("user_key") or "local")


def _login_required_redirect(request: Request) -> Response | None:
    settings = get_settings()
    user = _current_user(request)
    if settings.require_login and not user.get("authenticated"):
        next_path = str(request.url.path) + (("?" + request.url.query) if request.url.query else "")
        return RedirectResponse(f"/auth/login?next={urllib.parse.quote(_safe_next_path(next_path))}", status_code=status.HTTP_303_SEE_OTHER)
    if user.get("authenticated") and not _user_has_required_access(user):
        return _access_denied_response()
    return None


def _rewrite_endpoint_origin(endpoint: str, base_url: str) -> str:
    if not endpoint or not base_url:
        return endpoint
    parsed_endpoint = urllib.parse.urlsplit(endpoint)
    parsed_base = urllib.parse.urlsplit(base_url.strip().rstrip("/"))
    if not parsed_endpoint.scheme or not parsed_base.scheme or not parsed_base.netloc:
        return endpoint
    return urllib.parse.urlunsplit((parsed_base.scheme, parsed_base.netloc, parsed_endpoint.path, parsed_endpoint.query, parsed_endpoint.fragment))


def _oidc_discovery_document() -> dict[str, Any]:
    settings = get_settings()
    issuer = settings.oidc_public_issuer
    if not issuer:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="oidc_issuer_unconfigured")
    url = settings.oidc_discovery_url or urllib.parse.urljoin(issuer, ".well-known/openid-configuration")
    try:
        response = httpx.get(url, timeout=8.0)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="oidc_discovery_unavailable") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="oidc_discovery_invalid")
    normalized = dict(payload)
    normalized["issuer"] = issuer
    for field in ("authorization_endpoint", "end_session_endpoint"):
        if normalized.get(field):
            normalized[field] = _rewrite_endpoint_origin(str(normalized[field]), issuer)
    for field in ("token_endpoint", "userinfo_endpoint", "jwks_uri"):
        if normalized.get(field) and settings.oidc_backchannel_base_url:
            normalized[field] = _rewrite_endpoint_origin(str(normalized[field]), settings.oidc_backchannel_base_url)
    return normalized


def _oidc_callback_url(request: Request) -> str:
    settings = get_settings()
    if settings.oidc_redirect_uri:
        return settings.oidc_redirect_uri
    if settings.public_url:
        return f"{settings.public_url}/auth/callback"
    return str(request.url_for("auth_callback"))


def _auth_missing_envs() -> list[str]:
    settings = get_settings()
    missing: list[str] = []
    if not settings.session_secret:
        missing.append("OPENFEED_SESSION_SECRET or OPENFEED_SECRET_KEY")
    if not settings.oidc_issuer:
        missing.append("OPENFEED_OIDC_ISSUER")
    if not settings.oidc_client_id:
        missing.append("OPENFEED_OIDC_CLIENT_ID")
    if not settings.oidc_client_secret:
        missing.append("OPENFEED_OIDC_CLIENT_SECRET")
    return missing


def _auth_error_page(title: str, detail: str, missing: list[str] | None = None, status_code: int = 503) -> Response:
    missing_html = ""
    if missing:
        missing_html = "<ul>" + "".join(f"<li><code>{escape(item)}</code></li>" for item in missing) + "</ul>"
    content = f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{escape(title)} - OpenFeed</title>{_head_icons()}<style>:root{{--bg:#0a0a0b;--surface:#121214;--surface-2:#1a1a1d;--border:rgba(255,255,255,.09);--text:#ededef;--muted:#8b8b93;--accent:#e5484d;--r-panel:14px}}body{{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--text);font-family:Inter,system-ui,sans-serif}}main{{width:min(560px,calc(100vw - 32px));border:1px solid var(--border);border-radius:18px;background:var(--surface);padding:24px}}h1{{margin:0 0 10px;font-size:22px;font-weight:650;letter-spacing:-.022em}}p{{color:var(--muted);line-height:1.5}}code{{background:var(--surface-2);border-radius:6px;padding:2px 6px;color:var(--text)}}a{{color:var(--accent)}}</style></head><body><main><h1>{escape(title)}</h1><p>{escape(detail)}</p>{missing_html}<p><a href="/">Return to OpenFeed</a></p></main></body></html>'''
    return Response(content, status_code=status_code, media_type="text/html; charset=utf-8")


def _set_auth_session_cookie(response: Response, payload: dict[str, Any]) -> None:
    response.set_cookie(get_settings().session_cookie, _sign_payload(payload), max_age=AUTH_SESSION_TTL_SECONDS, httponly=True, secure=_cookie_secure(), samesite="lax", path="/")


def _clear_auth_session_cookie(response: Response) -> None:
    response.delete_cookie(get_settings().session_cookie, path="/", secure=_cookie_secure(), samesite="lax")


def _set_auth_state_cookie(response: Response, payload: dict[str, Any]) -> None:
    response.set_cookie(get_settings().oidc_state_cookie, _sign_payload(payload), max_age=AUTH_STATE_TTL_SECONDS, httponly=True, secure=_cookie_secure(), samesite="lax", path="/")


def _clear_auth_state_cookie(response: Response) -> None:
    response.delete_cookie(get_settings().oidc_state_cookie, path="/", secure=_cookie_secure(), samesite="lax")


def _auth_groups_from_userinfo(userinfo: dict[str, Any]) -> list[str]:
    groups = userinfo.get("groups") or userinfo.get("ak_groups") or []
    if not isinstance(groups, list):
        return []
    return sorted({str(group) for group in groups if str(group).strip()})



def _valid_uuid(value: str) -> bool:
    try:
        uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return False
    return True


def _text_excerpt(value: str, limit: int = 700) -> str:
    text = TAG_RE.sub(" ", unescape(value or ""))
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + "..."


def _description_text(value: str, limit: int = 12000) -> str:
    """Strip markup from a description while keeping the lines the author wrote.

    _text_excerpt collapses every run of whitespace into a single space. That is right for
    a one-line card subtitle and destructive here: chapter lists, link blocks and credits
    are structure somebody laid out deliberately, and they only survive if the newlines do.
    """
    text = unescape(value or "")
    # Markup that *is* a line break has to become one before the tags are stripped,
    # otherwise it collapses into a space along with everything else.
    text = re.sub(r"(?i)<\s*br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</\s*(p|div|li)\s*>", "\n", text)
    text = TAG_RE.sub(" ", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    kept: list[str] = []
    for line in lines:
        # One blank line separates blocks; more than that is just slack.
        if not line and (not kept or not kept[-1]):
            continue
        kept.append(line)
    text = "\n".join(kept).strip()
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + "..."


def _thumbnail_for(video: dict[str, Any]) -> str:
    """Serve every thumbnail through our own /vi/ proxy.

    Stored thumbnail URLs are absolute and point at whichever host produced them -
    i.ytimg.com for most, an Invidious instance for the rest - so rendering them
    directly both sends the viewer's browser (and IP) to Google on every card and
    breaks whenever the Invidious URL changes. Rebuilding the path from the video id
    keeps the request inside the stack, the same way avatars go through /ggpht/.
    """
    video_id = str(video.get("youtube_video_id") or "")
    thumb = str(video.get("thumbnail_url") or "")
    if not video_id:
        return thumb
    if not thumb or "/vi/" in thumb:
        return f"/vi/{video_id}/hqdefault.jpg"
    return thumb


def _youtube_feed_url(youtube_channel_id: str) -> str:
    return f"https://www.youtube.com/feeds/videos.xml?channel_id={youtube_channel_id}"


ALLOWED_FEEDBACK_EVENTS = {
    "hide",
    "not_interested",
    "undo_hide",
    "undo_not_interested",
    "less_like_this",
    "more_like_this",
    "like",
    "dislike",
    "clear_reaction",
    "mark_watched",
    "watch_page_open",
    # Recorded when a comment is voted on. Weighted far below a rating of the video
    # itself — agreeing with a comment is a hint about the video, not a verdict on it.
    "comment_like",
    "comment_dislike",
}


class CommentReactionRequest(BaseModel):
    youtube_video_id: str
    comment_id: str
    reaction: str = ""


class CommentAvatarsRequest(BaseModel):
    author_ids: list[str] = Field(default_factory=list)


class FeedbackRequest(BaseModel):
    youtube_video_id: str
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)


class SubscribeRequest(BaseModel):
    youtube_channel_id: str = ""
    title: str = ""
    feed_url: str = ""


class ChannelNotificationRequest(BaseModel):
    level: str = "all"


class ApiTokenRequest(BaseModel):
    name: str = "API token"
    # A read-only token can call GET/HEAD/OPTIONS only, enforced in the access middleware.
    read_only: bool = False


class PollVoteRequest(BaseModel):
    # None or negative clears the answer.
    choice_index: int | None = None


class DownloadJobRequest(BaseModel):
    height: int = 0
    container: str = "mp4"
    title: str = ""
    needs_merge: bool = False
    # 'local' hands the file to the browser; 'server' keeps it in the library.
    destination: str = "local"


class PlaybackPreferencesRequest(BaseModel):
    prefer_local: bool | None = None
    public_invidious_enabled: bool | None = None
    public_invidious_urls: list[str] | None = None
    youtube_fallback: bool | None = None
    source_order: list[str] | None = None
    disabled_sources: list[str] | None = None


class PlaybackReportRequest(BaseModel):
    source_id: str
    ok: bool
    youtube_video_id: str = ""


class PlaybackProgressRequest(BaseModel):
    youtube_video_id: str
    current_seconds: float | None = None
    duration_seconds: float | None = None
    event_type: str = "progress"
    # Seconds watched since the previous report. Sent separately from current_seconds,
    # which restarts at zero each page open and so cannot be differenced server-side.
    watch_delta_seconds: float = 0.0
    # A real reading from the player, in video time. Omitted by anything that cannot
    # actually see the playhead, so a guess never becomes a resume point.
    resume_seconds: float | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class PlaylistRequest(BaseModel):
    name: str
    description: str | None = None


class PlaylistItemRequest(BaseModel):
    youtube_video_id: str


class SubscriptionCollectionRequest(BaseModel):
    name: str
    description: str | None = None


class SubscriptionCollectionMemberRequest(BaseModel):
    subscription_id: str


class FeedPreferencesRequest(BaseModel):
    """A partial update: anything omitted keeps its stored value.

    Defaulting these to concrete values instead meant every caller had to echo back the
    whole preference set, and any caller that forgot a field silently reset it — toggling
    Shorts would have switched off the RYD privacy setting.
    """

    preferred_topics: list[str] | None = None
    muted_topics: list[str] | None = None
    prioritize_subscriptions: bool | None = None
    exclude_shorts: bool | None = None
    discovery_level: float | None = None
    show_comments: bool | None = None
    ryd_enabled: bool | None = None
    sponsorblock_enabled: bool | None = None
    sponsorblock_actions: dict[str, str] | None = None
    show_posts_in_feeds: bool | None = None
    muted_channels: list[str] | None = None


def _json_ready(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for row in rows:
        item: dict[str, Any] = {}
        for key, value in row.items():
            if isinstance(value, datetime):
                item[key] = value.isoformat()
            else:
                item[key] = value
        converted.append(item)
    return converted


def _json_ready_one(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return _json_ready([row])[0]


def _datetime_text(value: Any) -> str:
    if not isinstance(value, datetime):
        return ""
    return value.isoformat()


def _date_text(value: Any) -> str:
    if not isinstance(value, datetime):
        return ""
    date = value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return f"{date.strftime('%b')} {date.day}, {date.year}"


def _rss_date(value: Any) -> str:
    date = value if isinstance(value, datetime) else datetime.now(timezone.utc)
    if date.tzinfo is None:
        date = date.replace(tzinfo=timezone.utc)
    return format_datetime(date.astimezone(timezone.utc), usegmt=True)


def _redis_status() -> dict[str, Any]:
    settings = get_settings()
    try:
        client = redis.Redis.from_url(settings.redis_url, socket_timeout=2, socket_connect_timeout=2)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    try:
        return {"ok": bool(client.ping())}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        try:
            client.close()
        except Exception:
            pass


def _json_ready_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, list):
        return [_json_ready_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_ready_value(item) for key, item in value.items()}
    return value


def _timed_check(name: str, check: Any) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        result = dict(check() or {})
        ok = bool(result.pop("ok", True))
        return {"name": name, "ok": ok, "latency_ms": round((time.perf_counter() - started) * 1000), **result}
    except Exception as exc:
        return {
            "name": name,
            "ok": False,
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "error": str(exc)[:300],
        }


def _check_database() -> dict[str, Any]:
    return db.db_status()


def _check_redis() -> dict[str, Any]:
    return _redis_status()


def _rss_poller_status() -> dict[str, Any]:
    """Last direct-RSS sync result, for the health check and settings surfaces.

    Built from the newest ``rss`` sync run: ok only when that run succeeded,
    with its finish time and entry count so the settings page can say when the
    poller last ran and what it pulled in. Never raises — a status read must
    not take the page down with it.
    """
    try:
        run = db.latest_sync_run("rss")
    except Exception as exc:  # noqa: BLE001 - status reads degrade, they never fail
        return {"ok": False, "error": f"could not read sync history: {exc}"[:200]}
    if run is None:
        return {"ok": False, "error": "RSS poller has not run yet"}
    finished = run.get("finished_at") or run.get("started_at")
    # "ok" is the pre-cutover vocabulary; "success" is current. Both mean ran fine.
    if run.get("status") in ("success", "ok"):
        return {
            "ok": True,
            "last_run_at": _json_ready_value(finished),
            "subscriptions_count": run.get("subscriptions_count") or 0,
            "entries_count": run.get("entries_count") or 0,
        }
    return {
        "ok": False,
        "status": run.get("status"),
        "last_run_at": _json_ready_value(finished),
        "entries_count": run.get("entries_count") or 0,
        "error": str(run.get("message") or "last RSS sync did not succeed")[:300],
    }


def _check_rss_poller() -> dict[str, Any]:
    return _rss_poller_status()


def _check_invidious_api() -> dict[str, Any]:
    settings = get_settings()
    with httpx.Client(timeout=8.0, follow_redirects=True) as client:
        response = client.get(f"{settings.invidious_base_url}/api/v1/trending")
        response.raise_for_status()
        data = response.json()
    return {"ok": True, "base_url": settings.invidious_base_url, "items": len(data) if isinstance(data, list) else None}


def _check_invidious_player() -> dict[str, Any]:
    settings = get_settings()
    probe_video_id = "GPynHL1VNiA"
    fallback = "youtube" if settings.youtube_fallback_enabled else "off"
    try:
        with httpx.Client(timeout=10.0, follow_redirects=True) as client:
            embed = client.get(f"{settings.invidious_base_url}/embed/{probe_video_id}")
            embed.raise_for_status()
            body = embed.text
            manifest_match = re.search(r'<source src="([^"]*manifest/dash[^"]*)"', body)
            if not manifest_match:
                raise PlaybackError("Invidious embed did not include a companion DASH manifest")
            manifest_path = unescape(manifest_match.group(1))
            manifest = client.get(f"{settings.invidious_base_url}{manifest_path}")
            manifest.raise_for_status()
    except Exception as exc:
        # Almost always the companion failing to validate a PO token, which YouTube
        # answers with a 404 on every stream request. Say so instead of implying it
        # will clear up on its own — and report that playback still works via fallback.
        return {
            "ok": False,
            "video_id": probe_video_id,
            "error": str(exc),
            "likely_cause": "invidious-companion could not validate a PO token with YouTube",
            "playback_fallback": fallback,
            "fallback_serving": fallback != "off",
            "probe_short_circuited": playback_backend_degraded(),
        }
    return {
        "ok": True,
        "video_id": probe_video_id,
        "embed": "ok",
        "companion_manifest": "ok",
        "manifest_content_type": manifest.headers.get("content-type"),
        "playback_fallback": fallback,
    }


def _feed_status() -> dict[str, Any]:
    settings = get_settings()
    items = db.list_recommended_videos(50)
    discovery_count = sum(1 for item in items if item.get("source_kind") == "discovery")
    subscription_count = sum(1 for item in items if item.get("source_kind") == "subscription")
    return {
        "ok": bool(items),
        "url": f"{settings.public_url}/feeds/recommended.xml",
        "items": len(items),
        "discovery_items": discovery_count,
        "subscription_items": subscription_count,
    }


def full_status_payload() -> dict[str, Any]:
    checks = {
        "database": _timed_check("database", _check_database),
        "redis": _timed_check("redis", _check_redis),
        "rss_poller": _timed_check("rss_poller", _check_rss_poller),
        "invidious_api": _timed_check("invidious_api", _check_invidious_api),
        "invidious_player": _timed_check("invidious_player", _check_invidious_player),
        "recommended_feed": _timed_check("recommended_feed", _feed_status),
    }
    critical = ["database", "redis", "rss_poller", "invidious_api", "invidious_player", "recommended_feed"]
    counts = _json_ready_value(db.system_counts())
    return {
        "ok": all(bool(checks[name].get("ok")) for name in critical),
        "service": "OpenFeed",
        "version": __version__,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "checks": checks,
        # Not a critical check: yt-dlp is a fallback, and the stack is healthy without it.
        # It is reported so a failed high-quality download has an obvious place to look.
        "media_backends": {
            "invidious": bool(checks["invidious_api"].get("ok")),
            "ytdlp_enabled": get_settings().ytdlp_enabled,
            "ytdlp": ytdlp.version() or "",
            "ffmpeg": downloads.ffmpeg_version() or "",
            "downloads": downloads.disk_usage(),
            "active_downloads": downloads.manager.active_count(),
        },
        "counts": counts,
        "enrichment": _json_ready_value(db.enrichment_status()),
        "latest_sync_runs": _json_ready(db.list_sync_runs(5)),
        "retrieval_rollup": _json_ready(db.retrieval_attempt_rollup()),
        "recent_retrieval_attempts": _json_ready(db.list_recent_retrieval_attempts(12)),
    }


HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}


# One pooled client for every Invidious call. A fresh httpx.Client per request meant a
# new TCP handshake for each player asset and each media segment, which is most of the
# "video takes a while to start" delay.
_PROXY_CLIENT: httpx.Client | None = None
_PROXY_CLIENT_LOCK = threading.Lock()


def _invidious_client() -> httpx.Client:
    global _PROXY_CLIENT
    if _PROXY_CLIENT is None:
        with _PROXY_CLIENT_LOCK:
            if _PROXY_CLIENT is None:
                _PROXY_CLIENT = httpx.Client(
                    timeout=httpx.Timeout(60.0, connect=8.0),
                    follow_redirects=False,
                    limits=httpx.Limits(max_connections=96, max_keepalive_connections=48, keepalive_expiry=90.0),
                )
    return _PROXY_CLIENT


def _invidious_proxy_headers(request: Request) -> dict[str, str]:
    allowed = {"accept", "accept-language", "range", "if-range", "user-agent"}
    return {key: value for key, value in request.headers.items() if key.lower() in allowed}


def _response_headers(headers: httpx.Headers) -> dict[str, str]:
    blocked = HOP_BY_HOP_HEADERS | {"content-encoding", "content-length"}
    return {key: value for key, value in headers.items() if key.lower() not in blocked}


def proxy_invidious(request: Request, upstream_path: str):
    auth_redirect = _login_required_redirect(request)
    if auth_redirect:
        return auth_redirect
    settings = get_settings()
    if not settings.invidious_base_url:
        raise HTTPException(status_code=502, detail="Invidious is not configured")
    query = request.url.query
    url = f"{settings.invidious_base_url}/{upstream_path.lstrip('/')}"
    if query:
        url = f"{url}?{query}"
    client = _invidious_client()
    upstream_request = client.build_request(request.method, url, headers=_invidious_proxy_headers(request))
    try:
        upstream_response = client.send(upstream_request, stream=True)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Invidious request failed: {exc}") from exc
    headers = _response_headers(upstream_response.headers)
    location = headers.get("location")
    if location and location.startswith(settings.invidious_base_url):
        headers["location"] = location.replace(settings.invidious_base_url, "", 1) or "/"
    return StreamingResponse(
        upstream_response.iter_bytes(),
        status_code=upstream_response.status_code,
        headers=headers,
        # Only the response is closed — the pooled client is shared and stays open.
        background=BackgroundTask(upstream_response.close),
    )


# The only static files the app serves. An explicit map rather than a directory mount:
# the name from the URL never reaches the filesystem, so there is no path to traverse.
_STATIC_ASSETS: dict[str, str] = {
    "favicon-32.png": "image/png",
    "apple-touch-icon.png": "image/png",
    "openfeed-logo.png": "image/png",
}
_ASSET_DIR = Path(__file__).parent / "assets"


@app.get("/assets/{name}", include_in_schema=False)
@app.head("/assets/{name}", include_in_schema=False)
def static_asset(name: str) -> Response:
    media_type = _STATIC_ASSETS.get(name)
    if not media_type:
        raise HTTPException(status_code=404, detail="Not found")
    try:
        body = (_ASSET_DIR / name).read_bytes()
    except OSError:
        raise HTTPException(status_code=404, detail="Not found")
    # Fixed filenames with no content hash, so no `immutable`: a replaced file under
    # the same name must revalidate within the hour rather than sticking forever.
    return Response(body, media_type=media_type, headers={"cache-control": "public, max-age=3600"})


@app.get("/favicon.ico", include_in_schema=False)
@app.head("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    """Browsers ask for this path whatever the markup says, so answer it with the PNG
    rather than letting every page load carry a 404."""
    return static_asset("favicon-32.png")


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    database = db.db_status()
    cache = _redis_status()
    return {
        "ok": bool(database.get("ok") and cache.get("ok")),
        "service": "OpenFeed",
        "version": __version__,
        "database": database,
        "redis": cache,
        "rss_poller": _rss_poller_status(),
    }


@app.get("/api/status/full")
def api_status_full() -> dict[str, Any]:
    return full_status_payload()


def _status_metric(label: str, value: Any, hint: str = "", tone: str = "") -> str:
    tone_class = f" {tone}" if tone else ""
    hint_html = f"<small>{escape(hint)}</small>" if hint else ""
    return (
        f'<div class="stat-tile{tone_class}"><span class="stat-label">{escape(label)}</span>'
        f'<strong class="stat-value">{escape(str(value))}</strong>{hint_html}</div>'
    )


def _status_section(title: str, body: str, subtitle: str = "") -> str:
    sub = f"<p>{escape(subtitle)}</p>" if subtitle else ""
    return f'<section class="status-block"><div class="status-head"><h2>{escape(title)}</h2>{sub}</div>{body}</section>'


def _status_page_html(user_key: str = "local") -> str:
    """Every subsystem, on one page, in the app's own visual language."""
    status = full_status_payload()
    checks = status.get("checks") or {}
    counts = status.get("counts") or {}
    backends = status.get("media_backends") or {}
    storage = backends.get("downloads") or {}

    labels = {
        "database": "Postgres", "redis": "Redis", "rss_poller": "RSS poller",
        "invidious_api": "Invidious API", "invidious_player": "Invidious player",
        "recommended_feed": "Recommended feed",
    }
    cards = []
    for key, label in labels.items():
        item = checks.get(key) or {}
        ok = bool(item.get("ok"))
        detail = " &middot; ".join(
            f"{escape(str(field).replace('_', ' '))} {escape(str(item.get(field)))}"
            for field in ("latency_ms", "status_code", "items", "likely_cause", "error")
            if item.get(field) is not None
        )
        cards.append(
            f'<article class="check {"ok" if ok else "bad"}">'
            f'<div><h3>{escape(label)}</h3><p>{detail or "&nbsp;"}</p></div>'
            f'<span class="check-badge">{"OK" if ok else "FAIL"}</span></article>'
        )

    # --- personalization -----------------------------------------------------------
    try:
        watch = db.watch_time_profile(user_key)
        takeout_profile = db.takeout_profile(user_key)
        takeout_record = db.latest_takeout_import(user_key)
        token_count = len(db.list_api_tokens(user_key))
        download_count = len(db.list_downloads(user_key, "", 500))
    except Exception as exc:
        logger.warning("status personalization read failed: %s", exc)
        watch, takeout_profile, takeout_record, token_count, download_count = {}, {}, None, 0, 0

    hours = (watch.get("total_seconds") or 0) / 3600
    personalization = "".join([
        _status_metric("Watch time tracked", f"{hours:.1f} h", f"{len(watch.get('channels') or {})} channels"),
        _status_metric("Signal confidence", f"{(watch.get('confidence') or 0):.0%}", "full weight at 1 hour"),
        _status_metric("Imported history", f"{takeout_profile.get('total_watches') or 0:,}",
                       "Takeout" if takeout_record else "no import yet"),
        _status_metric("Already-watched IDs", f"{takeout_profile.get('watched_videos') or 0:,}", "suppressed in ranking"),
    ])

    # --- media backends ------------------------------------------------------------
    ytdlp_version = backends.get("ytdlp") or ""
    ffmpeg_version = (backends.get("ffmpeg") or "").split(" version ")[-1].split(" ")[0] if backends.get("ffmpeg") else ""
    free = storage.get("free_bytes")
    media = "".join([
        _status_metric("yt-dlp", ytdlp_version or "not installed", "fallback + merged downloads",
                       "ok" if ytdlp_version else "warn"),
        _status_metric("ffmpeg", ffmpeg_version or "not installed", "muxes split tracks",
                       "ok" if ffmpeg_version else "warn"),
        _status_metric("Downloads stored", f"{download_count:,}", _bytes_text(storage.get("bytes")) or "nothing yet"),
        _status_metric("Disk free", _bytes_text(free) if free else "unknown", str(storage.get("path") or "")),
        _status_metric("Active jobs", backends.get("active_downloads") or 0, "downloading or merging"),
        _status_metric("API tokens", token_count, "active, non-revoked"),
    ])

    # --- library -------------------------------------------------------------------
    enrichment = status.get("enrichment") or {}
    library = "".join(
        _status_metric(str(key).replace("_", " ").title(), f"{value:,}" if isinstance(value, int) else value)
        for key, value in list(counts.items())[:8]
    )
    enrichment_tiles = "".join(
        _status_metric(str(key).replace("_", " ").title(), f"{value:,}" if isinstance(value, int) else value)
        for key, value in list(enrichment.items())[:6]
    )

    # --- playback sources ----------------------------------------------------------
    source_rows = []
    try:
        for source in _all_playback_sources(user_key):
            health = source_health_snapshot(source["id"])
            cooling = source_in_cooldown(source["id"])
            state = "cooling down" if cooling else health.get("status", "unknown")
            source_rows.append(
                f'<tr><td>{escape(source["label"])}</td><td>{escape(source["kind"])}</td>'
                f'<td>{"enabled" if source.get("enabled", True) else "disabled"}</td>'
                f'<td class="{"bad-text" if cooling else ""}">{escape(str(state))}</td>'
                f'<td>{escape(str(health.get("failures") or 0))}</td></tr>'
            )
    except Exception as exc:
        logger.warning("status playback read failed: %s", exc)
    sources_table = (
        '<div class="status-scroll"><table><thead><tr><th>Source</th><th>Kind</th><th>State</th>'
        f'<th>Health</th><th>Failures</th></tr></thead><tbody>{"".join(source_rows) or "<tr><td colspan=5>No sources configured.</td></tr>"}</tbody></table></div>'
    )

    sync_rows = "".join(
        f'<tr><td>{escape(str(run.get("status") or ""))}</td>'
        f'<td>{escape(_relative_time(run.get("started_at")) or "-")}</td>'
        f'<td>{escape(str(run.get("subscriptions_count") or 0))}</td>'
        f'<td>{escape(str(run.get("entries_count") or 0))}</td>'
        f'<td>{escape(_text_excerpt(str(run.get("message") or ""), 90))}</td></tr>'
        for run in status.get("latest_sync_runs") or []
    ) or "<tr><td colspan=5>No sync runs yet.</td></tr>"

    retrieval_rows = "".join(
        f'<tr><td>{escape(str(item.get("source") or ""))}</td>'
        f'<td class="{"bad-text" if item.get("status") != "success" else ""}">{escape(str(item.get("status") or ""))}</td>'
        f'<td>{escape(_relative_time(item.get("attempted_at")) or "-")}</td>'
        f'<td>{escape(str(item.get("youtube_video_id") or ""))}</td>'
        f'<td>{escape(_text_excerpt(str(item.get("message") or ""), 80))}</td></tr>'
        for item in status.get("recent_retrieval_attempts") or []
    ) or "<tr><td colspan=5>No retrieval attempts recorded.</td></tr>"

    overall_ok = bool(status.get("ok"))
    generated = escape(_relative_time(status.get("generated_at")) or "just now")

    return f'''
      <section class="page-hero status-hero">
        <div>
          <p class="page-kicker">System</p>
          <h1>Status</h1>
          <p>Every subsystem OpenFeed depends on, checked {generated}.</p>
        </div>
        <span class="status-overall {"ok" if overall_ok else "bad"}">
          {_icon("verified" if overall_ok else "block")}
          <span>{"All systems healthy" if overall_ok else "Something needs attention"}</span>
        </span>
      </section>

      {_status_section("Service health", f'<div class="checks-grid">{"".join(cards)}</div>',
                       "The seven services a working install needs.")}
      {_status_section("Media backends", f'<div class="stat-grid">{media}</div>',
                       "Playback fallback, downloads and programmatic access.")}
      {_status_section("Personalization", f'<div class="stat-grid">{personalization}</div>',
                       "What the recommendation engine has to work with.")}
      {_status_section("Library", f'<div class="stat-grid">{library}</div>')}
      {_status_section("Enrichment", f'<div class="stat-grid">{enrichment_tiles}</div>',
                       "Metadata coverage. Topic ranking cannot see an unenriched video.")}
      {_status_section("Playback sources", sources_table,
                       "In preference order. A cooled-down source is retried automatically.")}
      {_status_section("Recent syncs",
                       f'<div class="status-scroll"><table><thead><tr><th>Status</th><th>Started</th><th>Subs</th><th>Entries</th><th>Message</th></tr></thead><tbody>{sync_rows}</tbody></table></div>')}
      {_status_section("Recent retrieval attempts",
                       f'<div class="status-scroll"><table><thead><tr><th>Source</th><th>Status</th><th>When</th><th>Video</th><th>Message</th></tr></thead><tbody>{retrieval_rows}</tbody></table></div>')}
      <p class="dialog-note">Machine-readable equivalent: <a href="/api/status/full">/api/status/full</a></p>
    '''


@app.get("/admin/status", response_class=HTMLResponse)
def admin_status(request: Request):
    """Kept as a redirect: status now lives in Settings, where the rest of the
    deployment-level information already is."""
    return RedirectResponse(url="/?settings=status", status_code=307)


def _unused_admin_status(request: Request):
    """The status page, rendered inside the app rather than as a separate document.

    It used to be a standalone page with its own palette, which meant the one screen an
    operator opens when something is wrong looked nothing like the app and shared none of
    its navigation. It now uses the same shell, and reports every subsystem rather than
    only the seven original health checks.
    """
    auth_redirect = _login_required_redirect(request)
    if auth_redirect:
        return auth_redirect
    user_key = _user_key(request)
    return _app_shell("Status", "status", _status_page_html(user_key), request=request)



# Icon geometry from Lucide v1.26.0 (ISC) — https://lucide.dev
# Vendored as path data so the app stays a single self-contained server render
# with no bundler, CDN request, or runtime dependency.

# One thumb, drawn once. Dislike is the same geometry rotated half a turn rather than
# a second hand-drawn path, so the two buttons can never drift out of sync. The outline
# is a single closed subpath, which means `fill: currentColor` alone turns it solid -
# that is how the selected state is rendered, with no second "filled" icon to maintain.
_THUMB_PATH = (
    '<path d="M15 5.88 14 10h5.83a2 2 0 0 1 1.92 2.56l-2.33 8A2 2 0 0 1 17.5 22H4a2 2 0 0 1-2-2v-8'
    'a2 2 0 0 1 2-2h2.76a2 2 0 0 0 1.79-1.11L12 2a3.13 3.13 0 0 1 3 3.88Z"/> <path d="M7 10v12"/>'
)

# Shared geometry for aliases: "later" is the clock, "collections" is the grid view.
# One variable each so the copies can never drift out of sync again.
_CLOCK_PATH = '<circle cx="12" cy="12" r="10"/> <path d="M12 6v6l4 2"/>'
_GRID_PATH = (
    '<rect width="7" height="7" x="3" y="3" rx="1"/> <rect width="7" height="7" x="14" y="3" rx="1"/>'
    ' <rect width="7" height="7" x="14" y="14" rx="1"/> <rect width="7" height="7" x="3" y="14" rx="1"/>'
)

ICON_PATHS = {
    "home": '<path d="M15 21v-8a1 1 0 0 0-1-1h-4a1 1 0 0 0-1 1v8"/> <path d="M3 10a2 2 0 0 1 .709-1.528l7-6a2 2 0 0 1 2.582 0l7 6A2 2 0 0 1 21 10v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
    "discover": '<circle cx="12" cy="12" r="10"/> <path d="m16.24 7.76-1.804 5.411a2 2 0 0 1-1.265 1.265L7.76 16.24l1.804-5.411a2 2 0 0 1 1.265-1.265z"/>',
    "search": '<path d="m21 21-4.34-4.34"/> <circle cx="11" cy="11" r="8"/>',
    "channels": '<path d="M3 5h.01"/> <path d="M3 12h.01"/> <path d="M3 19h.01"/> <path d="M8 5h13"/> <path d="M8 12h13"/> <path d="M8 19h13"/>',
    "library": '<path d="m16 6 4 14"/> <path d="M12 6v14"/> <path d="M8 8v12"/> <path d="M4 4v16"/>',
    "status": '<path d="M3 3v16a2 2 0 0 0 2 2h16"/> <path d="m19 9-5 5-4-4-3 3"/>',
    "play": '<path d="M5 5a2 2 0 0 1 3.008-1.728l11.997 6.998a2 2 0 0 1 .003 3.458l-12 7A2 2 0 0 1 5 19z"/>',
    "menu": '<path d="M4 5h16"/> <path d="M4 12h16"/> <path d="M4 19h16"/>',
    "more": '<circle cx="12" cy="12" r="1"/> <circle cx="12" cy="5" r="1"/> <circle cx="12" cy="19" r="1"/>',
    "tune": '<path d="M10 5H3"/> <path d="M12 19H3"/> <path d="M14 3v4"/> <path d="M16 17v4"/> <path d="M21 12h-9"/> <path d="M21 19h-5"/> <path d="M21 5h-7"/> <path d="M8 10v4"/> <path d="M8 12H3"/>',
    "bell": '<path d="M10.268 21a2 2 0 0 0 3.464 0"/> <path d="M3.262 15.326A1 1 0 0 0 4 17h16a1 1 0 0 0 .74-1.673C19.41 13.956 18 12.499 18 8A6 6 0 0 0 6 8c0 4.499-1.411 5.956-2.738 7.326"/>',
    "bell-ring": '<path d="M10.268 21a2 2 0 0 0 3.464 0"/> <path d="M22 8c0-2.3-.8-4.3-2-6"/> <path d="M3.262 15.326A1 1 0 0 0 4 17h16a1 1 0 0 0 .74-1.673C19.41 13.956 18 12.499 18 8A6 6 0 0 0 6 8c0 4.499-1.411 5.956-2.738 7.326"/> <path d="M4 2C2.8 3.7 2 5.7 2 8"/>',
    "bell-off": '<path d="M10.268 21a2 2 0 0 0 3.464 0"/> <path d="M17 17H4a1 1 0 0 1-.74-1.673C4.59 13.956 6 12.499 6 8a6 6 0 0 1 .258-1.742"/> <path d="m2 2 20 20"/> <path d="M8.668 3.01A6 6 0 0 1 18 8c0 2.687.77 4.653 1.707 6.05"/>',
    "user": '<path d="M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2"/> <circle cx="12" cy="7" r="4"/>',
    "clock": _CLOCK_PATH,
    "like": _THUMB_PATH,
    "dislike": f'<g transform="rotate(180 12 12)">{_THUMB_PATH}</g>',
    "hide": '<path d="M10.733 5.076a10.744 10.744 0 0 1 11.205 6.575 1 1 0 0 1 0 .696 10.747 10.747 0 0 1-1.444 2.49"/> <path d="M14.084 14.158a3 3 0 0 1-4.242-4.242"/> <path d="M17.479 17.499a10.75 10.75 0 0 1-15.417-5.151 1 1 0 0 1 0-.696 10.75 10.75 0 0 1 4.446-5.143"/> <path d="m2 2 20 20"/>',
    "check": '<path d="M20 6 9 17l-5-5"/>',
    "external": '<path d="M15 3h6v6"/> <path d="M10 14 21 3"/> <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>',
    "share": '<circle cx="18" cy="5" r="3"/> <circle cx="6" cy="12" r="3"/> <circle cx="18" cy="19" r="3"/> <line x1="8.59" x2="15.42" y1="13.51" y2="17.49"/> <line x1="15.41" x2="8.59" y1="6.51" y2="10.49"/>',
    "download": '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/> <path d="M7 10l5 5 5-5"/> <path d="M12 15V3"/>',
    "theater": '<rect x="2" y="5" width="20" height="14" rx="2"/> <path d="M2 9h20"/>',
    "shorts": '<path d="M4 14a1 1 0 0 1-.78-1.63l9.9-10.2a.5.5 0 0 1 .86.46l-1.92 6.02A1 1 0 0 0 13 10h7a1 1 0 0 1 .78 1.63l-9.9 10.2a.5.5 0 0 1-.86-.46l1.92-6.02A1 1 0 0 0 11 14z"/>',
    "playlist": '<path d="M21 5H3"/> <path d="M10 12H3"/> <path d="M10 19H3"/> <path d="M15 12.003a1 1 0 0 1 1.517-.859l4.997 2.997a1 1 0 0 1 0 1.718l-4.997 2.997a1 1 0 0 1-1.517-.86z"/>',
    "later": _CLOCK_PATH,
    "settings": '<path d="M9.671 4.136a2.34 2.34 0 0 1 4.659 0 2.34 2.34 0 0 0 3.319 1.915 2.34 2.34 0 0 1 2.33 4.033 2.34 2.34 0 0 0 0 3.831 2.34 2.34 0 0 1-2.33 4.033 2.34 2.34 0 0 0-3.319 1.915 2.34 2.34 0 0 1-4.659 0 2.34 2.34 0 0 0-3.32-1.915 2.34 2.34 0 0 1-2.33-4.033 2.34 2.34 0 0 0 0-3.831A2.34 2.34 0 0 1 6.35 6.051a2.34 2.34 0 0 0 3.319-1.915"/> <circle cx="12" cy="12" r="3"/>',
    "caption": '<rect width="18" height="14" x="3" y="5" rx="2" ry="2"/> <path d="M7 15h4M15 15h2M7 11h2M13 11h4"/>',
    "spark": '<path d="M11.017 2.814a1 1 0 0 1 1.966 0l1.051 5.558a2 2 0 0 0 1.594 1.594l5.558 1.051a1 1 0 0 1 0 1.966l-5.558 1.051a2 2 0 0 0-1.594 1.594l-1.051 5.558a1 1 0 0 1-1.966 0l-1.051-5.558a2 2 0 0 0-1.594-1.594l-5.558-1.051a1 1 0 0 1 0-1.966l5.558-1.051a2 2 0 0 0 1.594-1.594z"/> <path d="M20 2v4"/> <path d="M22 4h-4"/> <circle cx="4" cy="20" r="2"/>',
    "x": '<path d="M18 6 6 18"/> <path d="m6 6 12 12"/>',
    "subs": '<path d="M15.033 9.44a.647.647 0 0 1 0 1.12l-4.065 2.352a.645.645 0 0 1-.968-.56V7.648a.645.645 0 0 1 .967-.56z"/> <path d="M7 21h10"/> <rect width="20" height="14" x="2" y="3" rx="2"/>',
    "history": '<path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/> <path d="M3 3v5h5"/> <path d="M12 7v5l4 2"/>',
    "collections": _GRID_PATH,
    "grid-view": _GRID_PATH,
    "list-view": '<rect width="18" height="18" x="3" y="3" rx="2"/> <path d="M21 9H3"/> <path d="M21 15H3"/>',
    "verified": '<path d="M3.85 8.62a4 4 0 0 1 4.78-4.77 4 4 0 0 1 6.74 0 4 4 0 0 1 4.78 4.78 4 4 0 0 1 0 6.74 4 4 0 0 1-4.77 4.78 4 4 0 0 1-6.75 0 4 4 0 0 1-4.78-4.77 4 4 0 0 1 0-6.76Z"/> <path d="m9 12 2 2 4-4"/>',
    "chevron-right": '<path d="m9 18 6-6-6-6"/>',
    "chevron-left": '<path d="m15 18-6-6 6-6"/>',
    "chevron-up": '<path d="m18 15-6-6-6 6"/>',
    "chevron-down": '<path d="m6 9 6 6 6-6"/>',
    "mic": '<path d="M12 19v3"/> <path d="M19 10v2a7 7 0 0 1-14 0v-2"/> <rect x="9" y="2" width="6" height="13" rx="3"/>',
    "plus": '<path d="M5 12h14"/> <path d="M12 5v14"/>',
    "pencil": '<path d="M21.174 6.812a1 1 0 0 0-3.986-3.987L3.842 16.174a2 2 0 0 0-.5.83l-1.321 4.352a.5.5 0 0 0 .623.622l4.353-1.32a2 2 0 0 0 .83-.497z"/> <path d="m15 5 4 4"/>',
    "block": '<circle cx="12" cy="12" r="10"/> <path d="M4.929 4.929 19.07 19.071"/>',
    "minus-circle": '<circle cx="12" cy="12" r="10"/> <path d="M8 12h8"/>',
    "queue": '<path d="M16 5H3"/> <path d="M11 12H3"/> <path d="M16 19H3"/> <path d="M18 9v6"/> <path d="M21 12h-6"/>',
    "calendar": '<path d="M8 2v4"/> <path d="M16 2v4"/> <rect width="18" height="18" x="3" y="4" rx="2"/> <path d="M3 10h18"/>',
    "sort": '<path d="m21 16-4 4-4-4"/> <path d="M17 20V4"/> <path d="m3 8 4-4 4 4"/> <path d="M7 4v16"/>',
    "trash": '<path d="M10 11v6"/> <path d="M14 11v6"/> <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/> <path d="M3 6h18"/> <path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>',
}


def _brand_mark() -> str:
    """The OpenFeed logo.

    The real artwork, the same file the README uses, served at /assets/openfeed-logo.png.
    It survives 26px for the same reason the drawn mark it replaced did: one bold
    silhouette with a single counter and no small detail to lose. It carries its own
    gradient and its own transparency, so the tile behind it stays empty — a red plate
    under a red-to-pink mark would only muddy it.
    """
    return (
        '<span class="brand-mark" aria-hidden="true">'
        '<img src="/assets/openfeed-logo.png" alt="" width="26" height="26" decoding="async">'
        '</span>'
    )


def _icon(name: str) -> str:
    return f'<svg class="icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">{ICON_PATHS.get(name, "")}</svg>'


def _compact_count(value: Any) -> str:
    if value is None:
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if number < 1000:
        return str(int(number))
    for suffix in ("K", "M", "B"):
        number /= 1000
        if abs(number) < 1000:
            return f"{number:.1f}{suffix}".replace(".0", "")
    return f"{number:.1f}T".replace(".0", "")


def _duration_text(value: Any) -> str:
    try:
        seconds = int(value or 0)
    except (TypeError, ValueError):
        return ""
    if seconds <= 0:
        return ""
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _relative_time(value: Any) -> str:
    if not isinstance(value, datetime):
        return ""
    date = value
    if date.tzinfo is None:
        date = date.replace(tzinfo=timezone.utc)
    seconds = max(0, int((datetime.now(timezone.utc) - date.astimezone(timezone.utc)).total_seconds()))
    intervals = (
        (31536000, "year"),
        (2592000, "month"),
        (604800, "week"),
        (86400, "day"),
        (3600, "hour"),
        (60, "minute"),
    )
    for unit_seconds, label in intervals:
        if seconds >= unit_seconds:
            amount = seconds // unit_seconds
            suffix = "" if amount == 1 else "s"
            return f"{amount} {label}{suffix} ago"
    return "just now"


def _is_recent(value: Any) -> bool:
    if not isinstance(value, datetime):
        return False
    date = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - date.astimezone(timezone.utc)).total_seconds() <= 14 * 86400


def _published_date_is_decoded(value: Any) -> bool:
    """Whether a publish date was decoded from relative text rather than reported.

    Same signature the repair queue selects on: list endpoints yield "2 days ago"
    turned absolute at fetch time, so the timestamp carries the wall-clock of the
    fetch; the single-video endpoint reports a real calendar date, which lands at
    midnight UTC. A video showing a decoded date reads as freshly published and
    sorts wrong until something re-reads it.
    """
    if not isinstance(value, datetime):
        return False
    date = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    wall = date.astimezone(timezone.utc)
    return bool(wall.hour or wall.minute or wall.second or wall.microsecond)


def _avatar_initials(name: str) -> str:
    words = [part for part in re.split(r"\s+", name.strip()) if part]
    if not words:
        return "M"
    if len(words) == 1:
        return words[0][:2].upper()
    return (words[0][:1] + words[1][:1]).upper()


def _avatar_html(name: str, avatar_url: Any = None, *, css_class: str = "avatar", tag: str = "span", href: str = "", extra_attrs: str = "") -> str:
    """Channel avatar: the real YouTube profile picture when we have one, initials otherwise."""
    label = str(name or "Unknown")
    hue = _avatar_hue(label)
    url = str(avatar_url or "").strip()
    attrs = f' style="--avatar-hue:{hue}"{extra_attrs}'
    initials = escape(_avatar_initials(label))
    if url:
        # Initials stay in the DOM underneath, so a broken image falls back instead of going blank.
        inner = (
            f'<span class="avatar-initials">{initials}</span>'
            f'<img src="{escape(url, quote=True)}" alt="" loading="lazy" decoding="async" '
            f'onerror="this.closest(\'.avatar\').classList.remove(\'has-image\'); this.remove()">'
        )
        classes = f"{css_class} has-image"
    else:
        inner = f'<span class="avatar-initials">{initials}</span>'
        classes = css_class
    if tag == "a":
        return f'<a class="{classes}" href="{href}"{attrs} aria-label="{escape(label, quote=True)}">{inner}</a>'
    return f'<span class="{classes}"{attrs}>{inner}</span>'


def _avatar_hue(name: str) -> int:
    return sum(ord(char) for char in name) % 360


def _source_label(video: dict[str, Any]) -> str:
    return "Discovery" if video.get("source_kind") == "discovery" else "Library"


def _video_meta_parts(video: dict[str, Any]) -> list[str]:
    parts: list[str] = []
    views = _compact_count(video.get("view_count"))
    if views:
        parts.append(f"{views} views")
    relative = _relative_time(video.get("published_at"))
    if relative:
        parts.append(relative)
    return parts


SIDEBAR_VISIBLE_CHANNELS = 7


def _sidebar_channels(limit: int = 250) -> list[dict[str, Any]]:
    try:
        rows = db.list_subscriptions(limit)
    except Exception:
        return []
    channels: list[dict[str, Any]] = []
    for row in rows:
        channel_id = str(row.get("youtube_channel_id") or "").strip()
        if not channel_id:
            continue
        channels.append(row)
        if len(channels) >= limit:
            break
    return channels


def _sidebar_subscription_group(limit: int = SIDEBAR_VISIBLE_CHANNELS + 1) -> str:
    """Rendered on its own so /api/sidebar/subscriptions can swap it in after a
    subscription change without reloading the page.

    The shell renders only a SIDEBAR_VISIBLE_CHANNELS + 1 probe: enough to show the
    first screenful and to know whether more exist. The full list loads through
    /api/sidebar/subscriptions (limit=250) when it is actually expanded, so every
    page render does not pay for up to 750 subscription rows to draw seven links.
    """
    channels = _sidebar_channels(limit)
    if not channels:
        return ""
    rows = []
    for index, channel in enumerate(channels):
        name = str(channel.get("title") or "Channel")
        channel_id = escape(str(channel.get("youtube_channel_id") or ""))
        # The dot is not color-only: it carries sr-only "New" text so screen
        # readers announce fresh channels (aria-hidden would silence it).
        dot = '<span class="nav-dot"><span class="sr-only">New</span></span>' if _is_recent(channel.get("latest_video_at")) else ""
        avatar = _avatar_html(name, channel.get("channel_avatar_url") or channel.get("avatar_url"))
        # Everything past the first few is rendered but hidden, so "Show more" is instant
        # and needs no request. The shell only fetches a VISIBLE + 1 probe, so a capped
        # fetch means more may exist beyond what is rendered here — the count is exact
        # only when the fetch was not capped, and the full list arrives through
        # /api/sidebar/subscriptions when the group is expanded or refreshed.
        extra = " nav-channel-extra" if index >= SIDEBAR_VISIBLE_CHANNELS else ""
        hidden = " hidden" if index >= SIDEBAR_VISIBLE_CHANNELS else ""
        rows.append(
            f'<a class="nav-item nav-channel{extra}" href="/channel/{channel_id}"{hidden}>'
            f'{avatar}<span>{escape(name)}</span>{dot}</a>'
        )
    toggle = ""
    if len(channels) > SIDEBAR_VISIBLE_CHANNELS:
        if len(channels) >= limit:
            more_label = "Show more"
        else:
            remaining = len(channels) - SIDEBAR_VISIBLE_CHANNELS
            more_label = f"Show {remaining} more"
        toggle = (
            f'<button class="nav-item nav-more" type="button" data-subscription-more '
            f'data-more-label="{more_label}" data-less-label="Show less" aria-expanded="false">'
            f'{_icon("chevron-down")}<span>{more_label}</span></button>'
        )
    return (
        '<nav class="nav-group" data-subscription-nav>'
        f'<p class="nav-group-title">Subscriptions</p>{"".join(rows)}{toggle}</nav>'
    )


def _home_nav(active: str = "home") -> str:
    """The sidebar. Every entry goes to a page that *is* the thing it names.

    The old one kept sending you somewhere adjacent: History, Playlists and Liked all
    opened the Library page with a tab pre-selected, Collections opened the Subscriptions
    page, and Recommended opened Home — because `/recommendations` literally returned the
    home page. So clicking a thing landed you on a page that merely contained it, which
    is disorienting in a way that no amount of labelling fixes.

    Two entries are gone rather than repointed. "Library" was a container for four items
    that are all listed here individually, and "Recommended" was Home under another name.
    Both URLs still work and redirect, so old links and bookmarks survive.
    """
    groups: list[tuple[str, list[tuple[str, str, str, str]]]] = [
        ("", [
            ("home", "Home", "/", "home"),
            ("shorts", "Shorts", "/shorts", "shorts"),
            ("subscriptions", "Subscriptions", "/subscriptions", "subs"),
        ]),
        ("You", [
            ("history", "History", "/history", "history"),
            ("playlists", "Playlists", "/playlists", "playlist"),
            ("liked", "Liked videos", "/liked", "like"),
            ("downloads", "Downloads", "/downloads", "download"),
        ]),
        ("Channels", [
            ("channels", "All channels", "/channels", "channels"),
            ("collections", "Collections", "/channels?tab=collections", "collections"),
        ]),
    ]
    blocks: list[str] = []
    for heading, group in groups:
        items = []
        if heading:
            items.append(f'<p class="nav-heading">{escape(heading)}</p>')
        for key, label, href, icon in group:
            current = ' aria-current="page"' if key == active else ""
            item_class = " active" if key == active else ""
            items.append(f'<a class="nav-item{item_class}" href="{href}"{current}>{_icon(icon)}<span>{escape(label)}</span></a>')
        blocks.append(f'<nav class="nav-group">{"".join(items)}</nav>')

    blocks.append(_sidebar_subscription_group())
    return "".join(blocks)


def _mobile_nav(active: str = "home") -> str:
    # Labels match the sidebar verbatim and every entry goes to the page it names:
    # Subscriptions is /subscriptions (not /channels) and the library slot is
    # Playlists at /playlists. History keeps the history icon, not the library one.
    links = [
        ("home", "Home", "/", "home"),
        ("shorts", "Shorts", "/shorts", "shorts"),
        ("subscriptions", "Subscriptions", "/subscriptions", "subs"),
        ("history", "History", "/history", "history"),
        ("playlists", "Playlists", "/playlists", "playlist"),
    ]
    items = []
    for key, label, href, icon in links:
        current = ' aria-current="page"' if key == active else ""
        items.append(f'<a class="mobile-nav-item" href="{href}"{current}>{_icon(icon)}<span>{escape(label)}</span></a>')
    return "".join(items)


def _topic_tokens_from_videos(videos: list[dict[str, Any]], limit: int = 7) -> list[str]:
    counts: dict[str, int] = {}
    for video in videos:
        for token in db._recommendation_tokens(video)[:6]:
            counts[token] = counts.get(token, 0) + 1
    return [token for token, _ in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]]


def _topic_chips(videos: list[dict[str, Any]] | None = None) -> str:
    chips = [
        ("all", "All"),
        ("subscription", "From Subscriptions"),
        ("recent", "Recently Uploaded"),
        ("continue", "Continue Watching"),
        ("technology", "Technology"),
        ("news", "News"),
        ("education", "Education"),
        ("gaming", "Gaming"),
        ("music", "Music"),
        ("podcasts", "Podcasts"),
        ("long-form", "Long-Form"),
        ("short-videos", "Short Videos"),
    ]
    existing = {key for key, _ in chips}
    seen_labels = {label.lower() for _, label in chips}
    dynamic: list[str] = []
    try:
        # Real YouTube tags describe videos far better than words pulled from titles.
        dynamic = [str(row.get("tag") or "") for row in db.list_popular_tags(8) if row.get("tag")]
    except Exception:
        dynamic = []
    if not dynamic:
        dynamic = _topic_tokens_from_videos(videos or [], 7)
    for token in dynamic:
        key = f"topic:{token}"
        if key in existing or token.lower() in seen_labels:
            continue
        existing.add(key)
        seen_labels.add(token.lower())
        chips.append((key, token.title()))
    items = [
        f'<button class="topic-chip topic-action" type="button" data-open-feed-dialog aria-label="Personalize feed">{_icon("tune")}</button>',
        '<span class="topic-divider" aria-hidden="true"></span>',
    ]
    for key, label in chips:
        item_class = " active" if key == "all" else ""
        items.append(f'<button class="topic-chip{item_class}" type="button" data-filter="{escape(key)}">{escape(label)}</button>')
    return "".join(items)


def _creator_strip(videos: list[dict[str, Any]]) -> str:
    seen: set[str] = set()
    items: list[str] = []
    for video in videos:
        name = str(video.get("channel_title") or video.get("author") or "Unknown")
        channel_id = str(video.get("youtube_channel_id") or "")
        key = channel_id or name.lower()
        if key in seen:
            continue
        seen.add(key)
        href = f"/channel/{escape(channel_id)}" if channel_id else "/channels"
        initials = escape(_avatar_initials(name))
        label = escape(name)
        hue = _avatar_hue(name)
        items.append(
            f'<a class="creator-pill" href="{href}">{_avatar_html(name, video.get("channel_avatar_url"))}<span>{label}</span></a>'
        )
        if len(items) >= 12:
            break
    return "".join(items)


def _menu_row(icon: str, label: str, *, href: str = "", attrs: str = "", trailing: str = "", danger: bool = False, sub: str = "") -> str:
    classes = "menu-row"
    if danger:
        classes += " danger"
    if sub:
        classes += " stacked"
    copy = f'<span class="menu-label">{escape(label)}</span>'
    if sub:
        copy += f'<span class="menu-sub">{escape(sub)}</span>'
    # Always emit the trailing slot, even when empty: rows whose trailing text is set
    # later (the notification tick) need somewhere to write it.
    trailing_html = f'<span class="menu-trailing">{escape(trailing)}</span>'
    body = f'<span class="menu-icon">{_icon(icon)}</span><span class="menu-copy">{copy}</span>{trailing_html}'
    if href:
        rel = ' rel="noreferrer"' if href.startswith(("http://", "https://")) else ""
        return f'<a class="{classes}" href="{href}"{rel} role="menuitem"{attrs}>{body}</a>'
    return f'<button class="{classes}" type="button" role="menuitem"{attrs}>{body}</button>'


def _video_action_menu(video_id_raw: str, title_text: str, watch_href: str, original_url: str, invidious_url: str = "") -> str:
    video_id = escape(video_id_raw)
    title = escape(_text_excerpt(title_text, 72))
    rows = [
        _menu_row("play", "Watch in OpenFeed", href=watch_href),
        _menu_row("playlist", "Save to playlist", attrs=f' data-save-to-playlist="{video_id}" data-video-title="{escape(_text_excerpt(title_text, 90), quote=True)}"'),
        _menu_row("later", "Mark watched", attrs=f' data-feedback="mark_watched" data-video-id="{video_id}"'),
        _menu_row("share", "Share", attrs=f' data-copy-url="{watch_href}"'),
        '<div class="menu-separator" role="separator"></div>',
        _menu_row("like", "More like this", attrs=f' data-feedback="more_like_this" data-video-id="{video_id}"'),
        _menu_row("dislike", "Less like this", attrs=f' data-feedback="less_like_this" data-video-id="{video_id}"'),
        _menu_row("block", "Not interested", attrs=f' data-feedback="hide" data-video-id="{video_id}"', danger=True),
        '<div class="menu-separator" role="separator"></div>',
    ]
    if invidious_url:
        rows.append(_menu_row("external", "Open in Invidious", href=invidious_url))
    rows.append(_menu_row("external", "Open original", href=original_url))
    return f'''
      <details class="card-menu yt-menu">
        <summary class="menu-trigger" aria-label="Open actions for {title}">{_icon("more")}</summary>
        <div class="menu-panel" role="menu" aria-label="Video actions">{"".join(rows)}</div>
      </details>
    '''


def _subscription_feed_for_video(video: dict[str, Any]) -> str:
    """The feed URL proving this video's channel is subscribed, or "" if it is not.

    Subscription belongs to the channel, not to the route a particular video took to get
    here. A video pulled in by recommendations or search has no freshrss_entries row, so
    reading only the entry-level join reported "Subscribe" for channels that were already
    subscribed — and pressing that button re-added a feed that was already there.
    The channel-level column is therefore the authority, with the entry-level
    one kept as a fallback for rows fetched by queries that do not select it.
    """
    return str(
        video.get("channel_subscription_feed_url") or video.get("subscription_feed_url") or ""
    ).strip()


def _download_dialog() -> str:
    """Resolution picker for the download button. Rows are filled in from the API."""
    return f'''
      <dialog class="feed-dialog download-dialog" id="download-dialog" data-download-dialog aria-labelledby="download-dialog-title">
        <div class="dialog-head">
          <div>
            <h2 id="download-dialog-title">Download</h2>
            <p data-download-subtitle>Choose a quality.</p>
          </div>
          <button class="icon-button" type="button" data-close-dialog aria-label="Close">{_icon("x")}</button>
        </div>
        <div class="dialog-body">
          <div class="download-destination" data-download-destination role="radiogroup" aria-label="Where to save">
            <button class="destination-option active" type="button" role="radio" aria-checked="true" data-destination="local">
              <strong>Download to this device</strong>
              <small>Saves through your browser. The server copy is deleted afterwards.</small>
            </button>
            <button class="destination-option" type="button" role="radio" aria-checked="false" data-destination="server">
              <strong>Keep on server</strong>
              <small>Added to your Library, playable here without touching YouTube.</small>
            </button>
          </div>
          <div class="download-options" data-download-options role="group" aria-label="Available qualities">
            <p class="dialog-note">Checking what is available&hellip;</p>
          </div>
          <div class="download-progress" data-download-progress hidden>
            <div class="download-progress-head">
              <strong data-download-progress-label>Preparing&hellip;</strong>
              <button class="pill-action" type="button" data-download-cancel>Cancel</button>
            </div>
            <div class="download-bar"><span data-download-bar></span></div>
            <p class="dialog-note" data-download-stats></p>
          </div>
          <p class="dialog-note" data-download-note hidden></p>
        </div>
      </dialog>
    '''


# A timestamp in a description or comment: 1:23, 12:34, or 1:02:03.
_TIMESTAMP_RE = re.compile(r"(?<![\d:])((?:\d{1,2}:)?\d{1,2}:\d{2})(?![\d:])")
_URL_RE = re.compile(r"https?://[^\s<>\"]+")
_HANDLE_RE = re.compile(r"(?<![\w@/])@([A-Za-z0-9_.-]{3,30})\b")
_HASHTAG_RE = re.compile(r"(?<![\w&/])#([A-Za-z0-9_]{2,40})\b")


def _timestamp_seconds(label: str) -> int:
    parts = [int(piece) for piece in label.split(":")]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    return parts[0] * 60 + parts[1]


def _rich_text(raw: str) -> str:
    """Turn a creator's plain-text blurb back into the thing they wrote.

    YouTube descriptions carry real structure — chapter timestamps, links, line breaks
    people spent effort laying out — and collapsing that into one escaped paragraph threw
    all of it away. Escaping happens first and everything below only ever wraps existing
    escaped text, so no user content can become markup.
    """
    text = escape(raw)

    def _link(match: re.Match[str]) -> str:
        shown = match.group(0)
        # The href has to carry the real URL, but the visible text stays escaped.
        href = shown.replace("&amp;", "&")
        trimmed = shown.rstrip(".,);:!?")
        tail = shown[len(trimmed):]
        href = trimmed.replace("&amp;", "&")
        return (
            f'<a href="{href}" target="_blank" rel="noopener noreferrer nofollow ugc">{trimmed}</a>{tail}'
        )

    text = _URL_RE.sub(_link, text)
    text = _TIMESTAMP_RE.sub(
        lambda m: f'<button type="button" class="ts-link" data-seek="{_timestamp_seconds(m.group(1))}">{m.group(1)}</button>',
        text,
    )
    text = _HANDLE_RE.sub(
        lambda m: f'<a href="https://www.youtube.com/@{m.group(1)}" target="_blank" rel="noopener noreferrer nofollow ugc">@{m.group(1)}</a>',
        text,
    )
    text = _HASHTAG_RE.sub(lambda m: f'<a href="/search?q=%23{m.group(1)}">#{m.group(1)}</a>', text)
    # Line breaks are structure too — a blank line separates blocks, a single one wraps.
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>")


# YouTube reports a collaboration as one combined author string ("3Blue1Brown and Ben
# Syversen"), with a single channel id for whoever hosts it. Splitting it lets both names
# be shown, and lets the ones we happen to know about become links.
_CREATOR_SPLIT = re.compile(r"\s+(?:and|&|feat\.?|ft\.?|with|x)\s+|,\s+", re.IGNORECASE)
# Guarded so a channel legitimately named "Rock and Roll Garage" is not torn in half.
_MAX_CREATORS = 4
_MIN_CREATOR_NAME = 2


def split_creators(author: str) -> list[str]:
    """The individual creators behind a combined author string.

    Returns a single-element list for the ordinary case, so callers can treat everything
    uniformly. A split that produces something implausible - too many parts, or a part too
    short to be a channel name - is discarded in favour of the original string, because
    showing one correct name beats showing three wrong ones.
    """
    text = str(author or "").strip()
    if not text:
        return []
    parts = [part.strip() for part in _CREATOR_SPLIT.split(text) if part.strip()]
    if len(parts) < 2 or len(parts) > _MAX_CREATORS:
        return [text]
    if any(len(part) < _MIN_CREATOR_NAME for part in parts):
        return [text]
    return parts


def _creator_links(video: dict[str, Any], channel_href: str) -> str:
    """Render the creator line, linking each name we can resolve.

    The host channel keeps its link; a guest we have no id for is shown as plain text
    rather than a link that would go somewhere wrong.
    """
    author = str(video.get("channel_title") or video.get("author") or "")
    names = split_creators(author)
    if len(names) <= 1:
        return f'<a class="watch-channel-name" href="{channel_href}">{escape(author or "Unknown channel")}</a>'
    known = db.channel_ids_for_titles(names[1:]) if len(names) > 1 else {}
    rendered = [f'<a class="watch-channel-name" href="{channel_href}">{escape(names[0])}</a>']
    for name in names[1:]:
        target = known.get(name.strip().lower())
        if target:
            rendered.append(f'<a class="watch-channel-name" href="/channel/{escape(target, quote=True)}">{escape(name)}</a>')
        else:
            rendered.append(f'<span class="watch-channel-name is-plain">{escape(name)}</span>')
    return '<span class="watch-creators">' + '<span class="creator-sep">&amp;</span>'.join(rendered) + '</span>'


def _video_details_pane(video: dict[str, Any], download: dict[str, Any] | None = None) -> str:
    """The facts about a video that do not fit in a one-line meta row.

    Release date, exact figures, duration, captions, where the video came from and whether
    a copy is on disk. Collapsed by default so it is available without crowding the page,
    and skipped entirely when there is nothing worth showing.
    """
    rows: list[tuple[str, str]] = []

    published = video.get("published_at")
    exact = _date_text(published)
    relative = _relative_time(published)
    if exact:
        rows.append(("Released", f"{escape(exact)}" + (f" &middot; {escape(relative)}" if relative else "")))

    for label, key in (("Views", "view_count"), ("Likes", "like_count")):
        value = video.get(key)
        if isinstance(value, (int, float)) and value:
            # The exact figure, since the header already shows the rounded one.
            rows.append((label, f"{int(value):,}"))

    dislikes = video.get("dislike_count")
    if isinstance(dislikes, (int, float)) and dislikes:
        estimated = str(video.get("dislike_source") or "") == "ryd"
        rows.append(("Dislikes", f"{int(dislikes):,}" + (" <span class=\"detail-tag\">estimated</span>" if estimated else "")))

    duration = _duration_text(video.get("duration_seconds"))
    if duration:
        rows.append(("Duration", escape(duration)))

    captions = str(video.get("captions_status") or "").strip()
    if captions and captions != "unknown":
        rows.append(("Captions", escape(captions)))

    source_kind = str(video.get("source_kind") or "").strip()
    if source_kind:
        origin = "From a subscription feed" if source_kind == "subscription" else f"Discovered via {escape(str(video.get('discovery_source') or 'recommendations'))}"
        rows.append(("How it got here", origin))

    metadata_source = str(video.get("metadata_source") or "").strip()
    if metadata_source and metadata_source != "not fetched":
        fetched = _relative_time(video.get("metadata_fetched_at"))
        rows.append(("Metadata", escape(metadata_source) + (f" &middot; {escape(fetched)}" if fetched else "")))

    if download:
        quality = escape(str(download.get("quality") or ""))
        size = _bytes_text(download.get("size_bytes"))
        rows.append(("Downloaded", " &middot; ".join(part for part in (quality, size) if part) or "yes"))

    tags = [str(tag) for tag in (video.get("tags") or []) if str(tag).strip()][:12]
    tag_html = ""
    if tags:
        chips = "".join(f'<a class="detail-chip" href="/search?q={urllib.parse.quote(tag)}">{escape(tag)}</a>' for tag in tags)
        tag_html = f'<div class="detail-tags">{chips}</div>'

    if not rows and not tag_html:
        return ""

    row_html = "".join(f'<div class="detail-row"><dt>{escape(label)}</dt><dd>{value}</dd></div>' for label, value in rows)
    return f'''
      <details class="video-details">
        <summary>{_icon("status")}<span>Details</span></summary>
        <dl class="detail-list">{row_html}</dl>
        {tag_html}
      </details>
    '''


def _bytes_text(value: Any) -> str:
    try:
        size = float(value or 0)
    except (TypeError, ValueError):
        return ""
    if size <= 0:
        return ""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if size >= 10 or unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return ""


def _download_card(row: dict[str, Any], settings: Any) -> str:
    """A downloaded video, rendered as an ordinary card so it behaves like one.

    Reusing _home_video_card means the menu, the save-to-playlist action, the reaction
    buttons and the watch link all work exactly as they do everywhere else — a download
    is a video that happens to also exist on disk, not a separate kind of thing.
    """
    card = _home_video_card(row, 0, settings, force_watch_route=True)
    quality = escape(str(row.get("quality") or ""))
    size = _bytes_text(row.get("size_bytes"))
    meta = " &middot; ".join(part for part in (quality, size) if part)
    video_id = escape(str(row.get("youtube_video_id") or ""), quote=True)
    badge = (
        f'<span class="download-chip">{_icon("download")}<span>{meta}</span>'
        f'<button class="download-remove" type="button" data-remove-download="{video_id}" '
        f'aria-label="Remove download" title="Remove download">{_icon("trash")}</button></span>'
    )
    # Injected just inside the card so it sits over the thumbnail, without rebuilding the
    # whole card markup here and letting the two drift apart.
    return card.replace('<div class="video-info">', f'{badge}<div class="video-info">', 1)


def _watch_options_menu(video_id_raw: str, invidious_url: str, youtube_url: str, title_text: str = "") -> str:
    """The overflow menu for the action row.

    Download and Theater live here rather than on the row itself: YouTube keeps that row
    to the handful of actions people reach for constantly, and anything longer wraps onto
    a second line at ordinary window widths. Theater keeps its `t` shortcut either way.
    """
    video_id = escape(video_id_raw)
    title_attr = escape(_text_excerpt(title_text or video_id_raw, 90), quote=True)
    rows = [
        _menu_row("status", "Details", attrs=" data-open-details"),
        _menu_row("download", "Download", attrs=f' data-download-video="{video_id}" data-video-title="{title_attr}"'),
        _menu_row("theater", "Theater mode", trailing="t", attrs=" data-theater-toggle"),
        '<div class="menu-separator" role="separator"></div>',
        _menu_row("check", "Mark watched", attrs=f' data-feedback="mark_watched" data-video-id="{video_id}"'),
        _menu_row("history", "Start from the beginning", href=f"/watch/{video_id}?restart=1"),
        _menu_row("block", "Not interested", attrs=f' data-feedback="hide" data-video-id="{video_id}"', danger=True),
        '<div class="menu-separator" role="separator"></div>',
        _menu_row("share", "Copy OpenFeed link", attrs=f' data-copy-url="/watch/{video_id}"'),
        _menu_row("share", "Copy original link", attrs=f' data-copy-url="{youtube_url}"'),
        _menu_row("external", "Open in Invidious", href=invidious_url),
        _menu_row("external", "Open original", href=youtube_url),
    ]
    return f'''
      <details class="watch-menu yt-menu">
        <summary class="pill-action pill-icon" aria-label="More actions" title="More actions">{_icon("more")}<span class="sr-only">More actions</span></summary>
        <div class="menu-panel" role="menu" aria-label="Watch options">{"".join(rows)}</div>
      </details>
    '''


# Bell levels, in the order they appear in the menu. The icon doubles as the trigger's
# face so the current setting is readable without opening anything.
_NOTIFICATION_CHOICES = (
    ("all", "bell-ring", "All", "Every upload from this channel"),
    ("personalized", "bell", "Personalized", "Uploads matching topics you follow"),
    ("none", "bell-off", "None", "Nothing from this channel"),
)


def _notification_bell(channel_id_raw: str, level: str) -> str:
    """The bell beside Subscribed, for choosing how much this channel may notify you."""
    if not channel_id_raw:
        return ""
    channel_id = escape(channel_id_raw, quote=True)
    level = level if level in {choice[0] for choice in _NOTIFICATION_CHOICES} else "all"
    current = next(choice for choice in _NOTIFICATION_CHOICES if choice[0] == level)
    rows = [
        _menu_row(
            icon,
            label,
            sub=description,
            trailing="✓" if value == level else "",
            attrs=f' data-notify-level="{value}" data-channel-id="{channel_id}"',
        )
        for value, icon, label, description in _NOTIFICATION_CHOICES
    ]
    muted_class = " is-muted" if level == "none" else ""
    return f'''
      <details class="notify-menu yt-menu" data-notify-menu data-channel-id="{channel_id}">
        <summary class="notify-trigger{muted_class}" data-notify-trigger aria-label="Notifications: {current[2]}" title="Notifications: {current[2]}">{_icon(current[1])}{_icon("chevron-down")}</summary>
        <div class="menu-panel" role="menu" aria-label="Notification settings">{"".join(rows)}</div>
      </details>
    '''


def _subscribe_control(channel_id_raw: str, feed_url_raw: str, channel_text: str, subscribed: bool, notify_level: str = "all") -> str:
    """Subscribe pill, plus the notification bell once you are actually subscribed."""
    if not channel_id_raw or not feed_url_raw:
        return ""
    channel_id = escape(channel_id_raw, quote=True)
    feed_url = escape(feed_url_raw, quote=True)
    title_attr = escape(channel_text, quote=True)
    channel = escape(channel_text)
    if subscribed:
        button = (
            f'<button class="watch-subscribe subscribed" type="button" data-unsubscribe-feed data-channel-id="{channel_id}" '
            f'data-feed-url="{feed_url}" data-title="{title_attr}" aria-label="Unsubscribe from {channel}">'
            f'{_icon("check")}<span>Subscribed</span></button>'
        )
        group_class = "subscribe-group is-subscribed"
    else:
        button = (
            f'<button class="watch-subscribe" type="button" data-subscribe-feed data-channel-id="{channel_id}" '
            f'data-feed-url="{feed_url}" data-title="{title_attr}" aria-label="Subscribe to {channel}">'
            f'<span>Subscribe</span></button>'
        )
        group_class = "subscribe-group"
    # The bell is always rendered and hidden by the group class, so subscribing in place
    # reveals it without a page load or a second copy of this markup in JavaScript.
    bell = _notification_bell(channel_id_raw, notify_level)
    return f'<div class="{group_class}" data-subscribe-group>{button}{bell}</div>'


def _header_actions(user: dict[str, Any] | None = None, user_key: str = "local") -> str:
    settings = get_settings()
    user = user or _current_user(None)
    try:
        _, unread_count = _notification_items(user_key, 24)
    except Exception:
        unread_count = 0
    badge_text = "9+" if unread_count > 9 else (str(unread_count) if unread_count else "")
    badge_hidden = "" if unread_count else " hidden"
    signed_in = bool(user.get("authenticated"))
    display_name = str(user.get("display_name") or "Local OpenFeed")
    email = str(user.get("email") or "")
    initials = escape(_avatar_initials(display_name))
    auth_sub = str(user.get("sub") or "local")
    auth_status = "Authentik account" if signed_in else ("Local mode" if not settings.require_login else "Login required")
    auth_action = (
        _menu_row("external", "Sign out", href="/auth/logout")
        if signed_in
        else _menu_row("user", "Sign in with Authentik", href="/auth/login?next=/")
    )
    return f'''
      <div class="header-actions">
        <a class="icon-button compact-only" href="/search" aria-label="Open search">{_icon("search")}</a>
        <details class="header-menu yt-menu" data-notifications>
          <summary class="icon-button bell-wrap" aria-label="Open notifications">{_icon("bell")}<span class="bell-badge" data-bell-badge{badge_hidden}>{badge_text}</span></summary>
          <div class="menu-panel menu-panel-wide notifications-panel" role="menu" aria-label="Notifications">
            <div class="menu-heading"><span>Notifications</span><small data-notification-summary>From your subscriptions</small></div>
            <div data-notification-list><p class="notification-empty">Loading&hellip;</p></div>
          </div>
        </details>
        <details class="header-menu profile-menu yt-menu">
          <summary class="profile-dot" aria-label="Open OpenFeed account menu">{initials}</summary>
          <div class="menu-panel account-panel" role="menu" aria-label="Account menu">
            <div class="account-summary"><span class="profile-dot">{initials}</span><div><strong>{escape(display_name)}</strong><small>{escape(email or auth_status)}</small></div></div>
            <div class="menu-separator" role="separator"></div>
            {_menu_row("settings", "Settings", attrs=" data-open-settings-dialog")}
            {auth_action}
            <div class="menu-separator" role="separator"></div>
            {_menu_row("history", "History", href="/history")}
            {_menu_row("subs", "Subscriptions", href="/channels")}
            {_menu_row("status", "Status", href="/?settings=status")}
            <div class="menu-separator" role="separator"></div>
            {_menu_row("check", "User key", trailing=auth_sub[:22], attrs=" disabled").replace('class="menu-row"', 'class="menu-row disabled"')}
          </div>
        </details>
      </div>
    '''


def _feed_dialog(user_key: str = "local") -> str:
    """Feed controls, built on what the feed actually contains.

    The previous version showed a "Subscriptions 35% / New to you 65%" bar derived from
    the slider position — a restatement of the input dressed up as a measurement, which is
    worse than showing nothing because it invites you to trust it. Every number here is
    counted from the ranked feed instead, so the panel answers "what am I actually being
    shown" before it offers to change it.
    """
    from . import evaluation

    preferences = db.get_feed_preferences(user_key)
    preferred = ", ".join(preferences.get("preferred_topics") or [])
    muted = ", ".join(preferences.get("muted_topics") or [])
    muted_channels = {str(c).lower() for c in (preferences.get("muted_channels") or [])}
    prioritize_checked = " checked" if preferences.get("prioritize_subscriptions", True) else ""
    shorts_checked = " checked" if preferences.get("exclude_shorts", False) else ""
    discovery_level = max(0, min(100, int(float(preferences.get("discovery_level", 0.35)) * 100)))

    # Deliberately NOT computed here. Measuring the feed means running a full ranking
    # pass, and this dialog is rendered into every page whether or not anyone opens it —
    # so it was costing ~125ms on every single page load to draw a panel nobody had asked
    # for. The panels below load on open instead.
    coverage: dict[str, Any] = {}

    total = int(coverage.get("videos") or 0)
    sources = coverage.get("sources") or {}
    subs_count = int(sources.get("subscription") or 0)
    discovery_count = int(sources.get("discovery") or 0)
    subs_pct = round(subs_count / total * 100) if total else 0
    discovery_pct = 100 - subs_pct if total else 0
    creators = int(coverage.get("distinct_channels") or 0)
    top_share = float(coverage.get("top_channel_share") or 0)

    composition = (
        f'<div class="fc-measure">'
        f'<div class="fc-bar" role="img" aria-label="{subs_pct}% from subscriptions, {discovery_pct}% discovered">'
        f'<span class="fc-bar-fill" style="width:{subs_pct}%"></span></div>'
        f'<div class="fc-legend"><span><i class="fc-dot subs"></i>From channels you follow <b>{subs_pct}%</b></span>'
        f'<span><i class="fc-dot disc"></i>Discovered <b>{discovery_pct}%</b></span></div>'
        f'<p class="fc-caption">Counted from the {total} videos ranked for you right now &mdash; '
        f'{creators} different creators, and no single one holds more than {round(top_share * 100)}% of the feed.</p>'
        f'</div>'
    ) if total else '<p class="fc-caption" data-fc-loading>Measuring your feed\u2026</p>'

    # Which signals actually fired, with real coverage. The old panel listed static chips,
    # including one for Gorse, which has since been removed entirely.
    signal_labels = {
        "semantic_affinity": "Topic similarity",
        "subscription_topic_affinity": "Subscription topics",
        "watch_time_channel": "Channels you watch",
        "watch_time_topic": "Subjects you watch",
        "takeout_channel": "Imported history",
        "takeout_already_watched": "Already watched",
        "preferred_topic": "Followed topics",
        "muted_topic": "Muted topics",
        "discovery_balance": "Discovery balance",
        "quick_abandonment": "Videos you bounced off",
        "partial_watch_penalty": "Part-watched",
    }
    chips = []
    for name, share in (coverage.get("signal_coverage") or {}).items():
        label = signal_labels.get(name)
        if not label:
            continue
        chips.append(f'<span class="fc-signal"><b>{label}</b><i>{round(float(share) * 100)}%</i></span>')
    signals = "".join(chips) or '<span class="fc-signal muted"><b>Loading\u2026</b></span>'

    # Channel mutes, offered against the creators actually filling the feed.
    top_channels: list[dict[str, Any]] = []
    channel_rows = "".join(
        f'<button class="fc-channel{" is-muted" if str(row["key"]).lower() in muted_channels else ""}" type="button" '
        f'data-mute-channel="{escape(str(row["key"]), quote=True)}" '
        f'aria-pressed="{"true" if str(row["key"]).lower() in muted_channels else "false"}">'
        f'<span>{escape(str(row["title"]))}</span><small>{row["count"]}</small>'
        f'<i class="fc-channel-mark">{_icon("block")}</i></button>'
        for row in top_channels
    ) or '<p class="fc-caption" data-fc-loading>Loading creators\u2026</p>'

    return f"""
      <dialog class="feed-dialog fc-dialog" id="feed-dialog" aria-labelledby="feed-dialog-title">
        <form id="feed-preferences-form" method="dialog" data-tune-root>
          <div class="fc-head">
            <div>
              <p class="fc-kicker">Feed controls</p>
              <h2 id="feed-dialog-title">What you see, and why</h2>
            </div>
            <button class="icon-button" type="button" data-close-dialog aria-label="Close">{_icon("x")}</button>
          </div>

          <div class="fc-body">
            <section class="fc-section">
              <h3>Your feed right now</h3>
              <div data-fc-composition>{composition}</div>
            </section>

            <section class="fc-section">
              <h3>How far to wander <span class="fc-value" data-tune-value>{discovery_level}%</span></h3>
              <p class="fc-caption">Low keeps close to what you already watch. High lets less certain
                matches through. This changes ranking only &mdash; the split above is measured after.</p>
              <input class="fc-slider" type="range" min="0" max="100" step="5" name="discovery_level"
                     value="{discovery_level}" data-tune-input aria-label="Discovery level">
              <div class="fc-scale"><span>Familiar</span><span>Balanced</span><span>Adventurous</span></div>
            </section>

            <section class="fc-section">
              <h3>Sources</h3>
              <label class="toggle-row"><span><strong>Prioritize subscriptions</strong>
                <small>Give channels you follow a head start in ranking.</small></span>
                <input name="prioritize_subscriptions" type="checkbox"{prioritize_checked}></label>
              <label class="toggle-row"><span><strong>Exclude Shorts</strong>
                <small>Hide videos under 90 seconds from every recommendation section.</small></span>
                <input name="exclude_shorts" type="checkbox"{shorts_checked}></label>
            </section>

            <section class="fc-section">
              <h3>Topics</h3>
              <label class="fc-field"><span>More of</span>
                <input name="preferred_topics" type="text" value="{escape(preferred, quote=True)}"
                       placeholder="woodworking, astronomy"></label>
              <label class="fc-field"><span>Less of</span>
                <input name="muted_topics" type="text" value="{escape(muted, quote=True)}"
                       placeholder="drama, reaction"></label>
              <p class="fc-caption">Comma separated. Followed topics get a boost; muted topics take a
                heavy penalty but are not removed outright.</p>
            </section>

            <section class="fc-section">
              <h3>Creators in your feed</h3>
              <p class="fc-caption">Tap to mute. A muted creator is removed from recommendations
                entirely, not just pushed down &mdash; you stay subscribed.</p>
              <div class="fc-channels" data-fc-channels>{channel_rows}</div>
            </section>

            <section class="fc-section">
              <h3>Signals in play</h3>
              <p class="fc-caption">What the ranker actually used, and how much of the feed each one
                touched.</p>
              <div class="fc-signals" data-fc-signals>{signals}</div>
            </section>
          </div>

          <div class="fc-foot">
            <p id="feed-preferences-status" class="status-line"></p>
            <button class="pill-action" type="button" data-reset-feed-preferences>Reset</button>
            <button class="primary-action" type="submit">Save changes</button>
          </div>
        </form>
      </dialog>
    """


def _sponsorblock_settings_html(user_key: str = "local") -> str:
    """The SponsorBlock pane: master switch, per-category action, and the trade stated."""
    from . import sponsorblock

    if not get_settings().sponsorblock_enabled:
        return '<p class="dialog-note">SponsorBlock is disabled for this deployment.</p>'
    try:
        preferences = db.get_feed_preferences(user_key)
        on = bool(preferences.get("sponsorblock_enabled"))
        actions = sponsorblock.normalize_actions(preferences.get("sponsorblock_actions"))
    except Exception:
        on, actions = False, dict(sponsorblock.DEFAULT_ACTIONS)

    rows = []
    for key, label, blurb in sponsorblock.CATEGORIES:
        current = actions.get(key, "off")
        options = "".join(
            f'<option value="{value}"{" selected" if current == value else ""}>{text}</option>'
            for value, text in (("skip", "Auto-skip"), ("manual", "Show skip button"), ("off", "Do nothing"))
        )
        rows.append(
            f'<label class="sb-row"><span><strong>{escape(label)}</strong><small>{escape(blurb)}</small></span>'
            f'<select data-sponsorblock-category="{key}" aria-label="{escape(label)} action">{options}</select></label>'
        )

    return f'''
      <label class="toggle-row"><span><strong>SponsorBlock</strong>
        <small>Skip sponsor reads and other interruptions using the community segment database.</small></span>
        <input type="checkbox" data-setting-sponsorblock{" checked" if on else ""}></label>
      <p class="dialog-note privacy-note">Privacy trade-off: this asks a third party about the videos
        you open. It is kept as small as it can be. Lookups use SponsorBlock's hashed endpoint &mdash;
        OpenFeed sends only the first four characters of the video ID's SHA-256, and gets back segments
        for every video sharing that prefix, so SponsorBlock cannot tell which one you are watching.
        Requests go out from this server, never your browser, so your IP and session stay here, and
        results are cached so a video is asked about roughly once. Counts shown on videos are RYD's estimates, not figures published by YouTube. This is read-only: OpenFeed never
        submits segments, votes, or reports, none of which are possible without identifying you.</p>
      <div class="sb-categories" data-sponsorblock-categories{"" if on else " hidden"}>{"".join(rows)}</div>
    '''


def _settings_dialog(user: dict[str, Any] | None = None, user_key: str = "local") -> str:
    settings = get_settings()
    user = user or _current_user(None)
    try:
        _preferences = db.get_feed_preferences(user_key)
        comments_checked = " checked" if _preferences.get("show_comments") else ""
        # Stored as exclude_shorts; surfaced as the friendlier positive "Show Shorts".
        shorts_checked = "" if _preferences.get("exclude_shorts") else " checked"
        ryd_checked = " checked" if _preferences.get("ryd_enabled") else ""
        posts_checked = " checked" if _preferences.get("show_posts_in_feeds") else ""
    except Exception:
        comments_checked = ""
        shorts_checked = " checked"
        ryd_checked = ""
        posts_checked = ""

    # The trade-off is stated in full next to the switch rather than buried in docs: this
    # is the one setting that sends anything about what is in your library off the box.
    ryd_row = (
        '<label class="toggle-row"><span><strong>Estimated dislike counts</strong>'
        '<small>Fetch public dislike estimates from Return YouTube Dislike, which YouTube '
        'itself stopped publishing in 2021.</small></span>'
        f'<input type="checkbox" data-setting-ryd{ryd_checked}></label>'
        '<p class="dialog-note privacy-note">Privacy trade-off: turning this on tells a third '
        'party which video IDs your server is interested in. To keep that as small as possible, '
        'OpenFeed only ever reads from RYD, never submits to it &mdash; your likes and dislikes '
        'stay in your own database. Most lookups run in the background on the sync schedule. '
        'Opening a video with no estimate yet does trigger one, but it is shuffled into a batch '
        'of unrelated videos that also need filling in, so RYD sees a group and cannot tell '
        'which one you are watching; if there is nothing to hide it among, the request waits for '
        'the next background pass instead. Everything goes out from the server, so RYD never '
        'sees your IP, account, or session, and each video is asked about once and then cached.</p>'
    ) if get_settings().ryd_enabled else (
        '<p class="dialog-note">Estimated dislike counts are disabled for this deployment.</p>'
    )
    signed_in = bool(user.get("authenticated"))
    display_name_raw = str(user.get("display_name") or "Local OpenFeed")
    display_name = escape(display_name_raw)
    email = escape(str(user.get("email") or ""))
    sub = escape(str(user.get("sub") or "local"))
    groups = user.get("groups") if isinstance(user.get("groups"), list) else []
    group_html = "".join(f"<span>{escape(str(group))}</span>" for group in groups[:8]) or "<span>No groups shared</span>"
    auth_badge = "Signed in" if signed_in else "Local mode"
    auth_copy = "Using your Authentik account. OpenFeed feedback, playlists, collections, and feed preferences are scoped to this account." if signed_in else "Using local fallback state. Sign in to connect this browser to your configured identity-provider account."
    auth_action = '<a class="primary-action" href="/auth/logout">Sign out</a>' if signed_in else '<a class="primary-action" href="/auth/login?next=/">Sign in with Authentik</a>'
    configured = "Configured" if settings.auth_configured else "Needs client settings"
    require_login = "Required" if settings.require_login else "Optional"
    required_group = escape(settings.required_group or "None")
    authorized = "Allowed" if _user_has_required_access(user) else "Missing group"
    rss_status = _rss_poller_status()
    if rss_status.get("ok"):
        last_run = rss_status.get("last_run_at") or "recently"
        rss_poller = f"OK — last sync {last_run} ({rss_status.get('entries_count') or 0} entries)"
    else:
        rss_poller = f"Not synced ({rss_status.get('error') or 'no sync runs recorded'})"
    panes = [
        ("playback", "Playback", "play"),
        ("content", "Content", "home"),
        ("import", "Import", "spark"),
        ("account", "Account", "user"),
        ("services", "Services", "status"),
        ("status", "Status", "spark"),
    ]
    nav = "".join(
        f'<button class="settings-nav-item{" active" if index == 0 else ""}" type="button" role="tab" '
        f'aria-selected="{"true" if index == 0 else "false"}" data-settings-pane="{key}">{_icon(icon)}<span>{escape(label)}</span></button>'
        for index, (key, label, icon) in enumerate(panes)
    )
    return f'''
      <dialog class="feed-dialog settings-dialog" id="settings-dialog" aria-labelledby="settings-dialog-title">
        <div class="settings-shell">
          <aside class="settings-nav" role="tablist" aria-label="Settings sections">
            <div class="settings-nav-head">
              <p class="tune-eyebrow">OpenFeed</p>
              <h2 id="settings-dialog-title">Settings</h2>
            </div>
            {nav}
            <div class="settings-nav-foot">
              <span class="profile-dot">{escape(_avatar_initials(display_name_raw))}</span>
              <div><strong>{display_name}</strong><small>{email or auth_badge}</small></div>
            </div>
          </aside>
          <div class="settings-content">
            <button class="icon-button settings-close" type="button" data-close-dialog aria-label="Close settings">{_icon("x")}</button>

            <section class="settings-pane active" data-settings-pane-body="playback" role="tabpanel">
              <header class="settings-pane-head">
                <h3>Playback &amp; fallback</h3>
                <p>OpenFeed plays from the first source that works and moves down the list when one fails. Drag to set the order, or turn a source off entirely.</p>
              </header>
              <div class="source-list" data-playback-sources aria-label="Playback sources">
                <p class="dialog-note">Loading sources&hellip;</p>
              </div>
              <div class="instance-editor" data-instance-editor hidden>
                <p class="field-label">Public instances</p>
                <div class="topic-chip-list" data-instance-chips></div>
                <div class="instance-add">
                  <input class="field-input" type="url" placeholder="yewtu.be" data-instance-input aria-label="Add an Invidious instance">
                  <button class="text-action" type="button" data-instance-add>{_icon("plus")}<span>Add</span></button>
                </div>
                <p class="dialog-note">Public instances see which videos you watch, and their reachability varies. Test them before relying on one.</p>
              </div>
              <div class="settings-pane-actions">
                <button class="primary-action" type="button" data-playback-test>Test all sources</button>
                <button class="text-action" type="button" data-playback-diagnose>{_icon("status")}<span>Diagnose playback</span></button>
                <span class="status-line" data-playback-status></span>
              </div>
              <div class="diagnosis" data-playback-diagnosis hidden></div>

              <div class="settings-divider" role="separator"></div>
              {_sponsorblock_settings_html(user_key)}
            </section>

            <section class="settings-pane" data-settings-pane-body="content" role="tabpanel">
              <header class="settings-pane-head">
                <h3>Content</h3>
                <p>What shows up in your feed and on watch pages.</p>
              </header>
              <label class="toggle-row"><span><strong>Show Shorts</strong><small>Include vertical Shorts, with their own shelf on Home. Off hides them everywhere.</small></span><input type="checkbox" data-setting-shorts{shorts_checked}></label>
              <label class="toggle-row"><span><strong>Show comments</strong><small>Load comments on watch pages through Invidious, only when you open a video.</small></span><input type="checkbox" data-setting-comments{comments_checked}></label>
              <p class="dialog-note" data-comments-status></p>
              <label class="toggle-row"><span><strong>Show posts in feeds</strong>
                <small>Mix community posts from subscribed channels into Home and Subscriptions.
                       The Posts tab on a channel works either way.</small></span>
                <input type="checkbox" data-setting-posts{posts_checked}></label>
              {ryd_row}
            </section>

            <section class="settings-pane" data-settings-pane-body="import" role="tabpanel">
              {_takeout_pane_html(user_key)}
            </section>

            <section class="settings-pane settings-pane-wide" data-settings-pane-body="status" role="tabpanel">
              <div data-status-body><p class="dialog-note">Loading status&hellip;</p></div>
            </section>

            <section class="settings-pane" data-settings-pane-body="account" role="tabpanel">
              <header class="settings-pane-head">
                <h3>Account</h3>
                <p>{escape(auth_copy)}</p>
              </header>
              <div class="settings-identity">
                <span class="profile-dot lg">{escape(_avatar_initials(display_name_raw))}</span>
                <div>
                  <strong>{display_name}</strong>
                  <small>{email or auth_badge}</small>
                  <div class="signal-list">{group_html}</div>
                </div>
              </div>
              <div class="settings-pane-actions">{auth_action}</div>
              <dl class="settings-list"><dt>Mode</dt><dd>{escape(settings.auth_mode)}</dd><dt>OIDC</dt><dd>{configured}</dd><dt>Login policy</dt><dd>{require_login}</dd><dt>Required group</dt><dd><code>{required_group}</code></dd><dt>Access</dt><dd>{authorized}</dd><dt>Subject</dt><dd><code>{sub}</code></dd></dl>
            </section>

            <section class="settings-pane" data-settings-pane-body="services" role="tabpanel">
              <header class="settings-pane-head">
                <h3>Services</h3>
                <p>The backends OpenFeed depends on.</p>
              </header>
              <dl class="settings-list"><dt>RSS poller</dt><dd>{escape(rss_poller)}</dd><dt>Invidious</dt><dd>{escape(settings.invidious_public_url or "Not configured")}</dd></dl>
              
            </section>
          </div>
        </div>
      </dialog>
    '''


def _progress_rail(video: dict[str, Any]) -> str:
    try:
        percent = float(video.get("percent_completed") or 0)
    except (TypeError, ValueError):
        return ""
    if percent <= 0:
        return ""
    width = max(2, min(100, percent))
    return f'<span class="progress-rail" aria-hidden="true"><span style="width:{width:.0f}%"></span></span>'


def _home_video_card(video: dict[str, Any], position: int, settings: Any, variant: str = "grid", force_watch_route: bool = False) -> str:
    title_text = str(video.get("title") or "Untitled")
    title = escape(title_text)
    channel_text = str(video.get("channel_title") or video.get("author") or "Unknown channel")
    channel = escape(channel_text)
    video_id_raw = str(video.get("youtube_video_id") or "")
    video_id = escape(video_id_raw)
    # Shorts open in the immersive Shorts feed; everything else opens the watch page.
    # A downloaded short overrides that: the Shorts feed streams from Invidious, so
    # sending it there would ignore the copy on disk the user asked us to keep.
    if not video_id:
        watch_href = "#"
    elif db.is_short_video(video) and not force_watch_route:
        watch_href = f"/shorts?start={video_id}"
    else:
        watch_href = f"/watch/{video_id}"
    channel_id = escape(str(video.get("youtube_channel_id") or ""))
    channel_href = f"/channel/{channel_id}" if channel_id else "/channels"
    original_url = escape(video.get("url") or f"https://www.youtube.com/watch?v={video_id_raw}")
    invidious_url = escape(f"{settings.invidious_public_url}/watch?v={video_id_raw}") if video_id_raw and settings.invidious_public_url else ""
    thumbnail = escape(_thumbnail_for(video))
    thumb_img = f'<img src="{thumbnail}" alt="" loading="lazy" onerror="this.closest(\'.thumb\').classList.add(\'no-thumb\'); this.remove()">' if thumbnail else ""
    duration = escape(_duration_text(video.get("duration_seconds")))
    duration_html = f'<span class="duration">{duration}</span>' if duration else ""
    progress_html = _progress_rail(video)
    source = escape(str(video.get("source_kind") or "subscription"))
    source_label = escape(_source_label(video))
    meta = " &middot; ".join(escape(part) for part in _video_meta_parts(video))
    initials = escape(_avatar_initials(channel_text))
    hue = _avatar_hue(channel_text)
    verified = _icon("verified").replace('class="icon"', 'class="icon verified"') if video.get("source_kind") != "discovery" else ""
    read_state = "read" if video.get("is_read") else "unread"
    recent = "true" if _is_recent(video.get("published_at")) else "false"
    duration_value = int(video.get("duration_seconds") or video.get("stored_duration_seconds") or 0)
    duration_class = "short" if 0 < duration_value <= 90 else "long" if duration_value >= 1200 else "standard"
    topic_tokens = " ".join(str(token) for token in (video.get("topic_tokens") or db._recommendation_tokens(video))[:8])
    menu = _video_action_menu(video_id_raw, title_text, watch_href, original_url, invidious_url)
    common_attrs = f'data-video-card data-video-id="{video_id}" data-channel-id="{channel_id}" data-source="{source}" data-recent="{recent}" data-read="{read_state}" data-duration-class="{duration_class}" data-topics="{escape(topic_tokens, quote=True)}"'
    if variant == "mini":
        return f'''
        <article class="video-card video-card-mini" {common_attrs}>
          <a class="thumb" href="{watch_href}" aria-label="Watch {title}"><span class="thumb-fallback">OpenFeed</span>{thumb_img}{duration_html}{progress_html}</a>
          <div class="card-copy">
            <h3><a href="{watch_href}">{title}</a></h3>
            <p><a href="{channel_href}">{channel}</a></p>
            <p>{meta or source_label}</p>
          </div>
          {menu}
        </article>
        '''
    card_class = "video-card video-card-featured" if variant == "featured" else "video-card"
    return f'''
    <article class="{card_class}" {common_attrs}>
      <a class="thumb" href="{watch_href}" aria-label="Watch {title}">
        <span class="thumb-fallback">OpenFeed</span>
        {thumb_img}
        {duration_html}
        {progress_html}
      </a>
      <div class="video-info">
        {_avatar_html(channel_text, video.get("channel_avatar_url"), tag="a", href=channel_href)}
        <div class="card-copy">
          <div class="title-row">
            <h3><a href="{watch_href}">{title}</a></h3>
            {menu}
          </div>
          <p class="channel-line"><a href="{channel_href}">{channel}</a>{verified}</p>
          <p class="metadata">{meta or source_label}</p>
        </div>
      </div>
    </article>
    '''


def _shelf_html(
    shelf_id: str,
    title: str,
    videos: list[dict[str, Any]],
    settings: Any,
    dismissible: bool = True,
    eyebrow: str = "",
    kind: str = "",
) -> str:
    cards = "".join(_home_video_card(video, index, settings, "featured") for index, video in enumerate(videos, start=1))
    close_button = (
        f'<button class="icon-button" type="button" data-dismiss-shelf aria-label="Hide this shelf">{_icon("x")}</button>'
        if dismissible else ""
    )
    kind_attr = f' data-section-kind="{escape(kind)}"' if kind else ""
    eyebrow_html = f'<p class="section-eyebrow">{escape(eyebrow)}</p>' if eyebrow else ""
    return f'''
    <section class="section-block" data-feed-section="{escape(shelf_id)}"{kind_attr} aria-labelledby="shelf-{escape(shelf_id)}">
      <div class="shelf" data-shelf>
        <div class="shelf-head">
          <div class="section-heading-copy">
            {eyebrow_html}
            <h2 id="shelf-{escape(shelf_id)}">{escape(title)}</h2>
          </div>
          {close_button}
        </div>
        <div class="shelf-viewport">
          <button class="shelf-nav prev" type="button" data-shelf-prev aria-label="Scroll left" hidden>{_icon("chevron-left")}</button>
          <div class="shelf-track" data-shelf-track>{cards}</div>
          <button class="shelf-nav next" type="button" data-shelf-next aria-label="Scroll right">{_icon("chevron-right")}</button>
        </div>
      </div>
    </section>
    '''


def _dedupe_section_videos(
    videos: list[dict[str, Any]], seen: set[str], limit: int, spread_channels: bool = True
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for video in videos:
        video_id = str(video.get("youtube_video_id") or "")
        if not video_id or video_id in seen:
            continue
        seen.add(video_id)
        selected.append(video)
        if len(selected) >= limit:
            break
    if spread_channels:
        return _spread_channels(selected)
    return selected


# How far apart two videos from the same channel must sit in a shelf. The grid
# is three columns, so 5 means repeats never share a row; when the pool is
# mostly one channel the rule relaxes rather than starving the shelf.
_CHANNEL_SPREAD_GAP = 5


def _video_channel_key(video: dict[str, Any]) -> str:
    return str(video.get("youtube_channel_id") or video.get("channel_title") or video.get("author") or "")


def _spread_channels(videos: list[dict[str, Any]], min_gap: int = _CHANNEL_SPREAD_GAP) -> list[dict[str, Any]]:
    """Reorder a shelf so one channel can't clog neighboring slots.

    Greedy lookahead in score order: each position takes the highest-ranked
    remaining video whose channel is absent from the previous ``min_gap - 1``
    picks. When every remaining video violates the gap (a pool dominated by
    one channel), the constraint relaxes to the video whose channel was seen
    longest ago, so repeats stay as far apart as the pool allows and the
    shelf still fills in merit order instead of starving. Videos with no
    channel identity are never constrained.
    """
    remaining = list(videos)
    ordered: list[dict[str, Any]] = []
    last_seen: dict[str, int] = {}
    while remaining:
        threshold = len(ordered) - min_gap
        pick = None
        for index, video in enumerate(remaining):
            key = _video_channel_key(video)
            if not key or last_seen.get(key, -10**9) <= threshold:
                pick = index
                break
        if pick is None:
            oldest = 10**18
            for index, video in enumerate(remaining):
                last = last_seen.get(_video_channel_key(video), -10**9)
                if last < oldest:
                    oldest = last
                    pick = index
        chosen = remaining.pop(pick if pick is not None else 0)
        key = _video_channel_key(chosen)
        if key:
            last_seen[key] = len(ordered)
        ordered.append(chosen)
    return ordered


def _feed_title_for_filter(feed_filter: str) -> str:
    labels = {
        "all": "Recommended for you",
        "subscription": "From your subscriptions",
        "recent": "Recently uploaded",
        "continue": "Continue watching",
        "technology": "Technology",
        "news": "News",
        "education": "Education",
        "gaming": "Gaming",
        "music": "Music",
        "podcasts": "Podcasts",
        "long-form": "Long-form videos",
        "short-videos": "Short videos",
    }
    key = (feed_filter or "all").strip().lower()
    if key.startswith("topic:"):
        return key.split(":", 1)[1].replace("-", " ").title()
    return labels.get(key, "Recommended for you")


def _missed_subscription_videos(limit: int = 8, user_key: str = "local", pool: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Subscription videos worth catching up on.

    Prefers entries still marked unread. New entries arrive unread, and marking
    everything read on import would otherwise leave this shelf permanently
    empty, so fall back to videos with no OpenFeed watch progress.
    """
    rows = pool if pool is not None else db.list_subscription_recommendations(max(32, limit * 4), user_key)
    unread: list[dict[str, Any]] = []
    unwatched: list[dict[str, Any]] = []
    for row in rows:
        if row.get("percent_completed"):
            continue
        if not row.get("is_read"):
            unread.append(row)
        unwatched.append(row)
    missed = unread if len(unread) >= 3 else unwatched
    return missed[:limit]


def _without_shorts(videos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Shorts belong in the Shorts shelf and the Shorts player, not in a 16:9 grid."""
    return [video for video in videos if not db.is_short_video(video)]


def _video_age_days(video: dict[str, Any]) -> float:
    published = video.get("published_at")
    if not isinstance(published, datetime):
        return 0.0
    stamp = published if published.tzinfo else published.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - stamp.astimezone(timezone.utc)).total_seconds() / 86400)


def _video_duration(video: dict[str, Any]) -> int:
    try:
        return int(video.get("duration_seconds") or video.get("stored_duration_seconds") or 0)
    except (TypeError, ValueError):
        return 0


# Catch-up is not one permanent shelf any more: each entry is a different slice of
# the same "you have not watched this yet" pool, and the feed rotates through them.
_CATCH_UP_SHELVES: list[dict[str, str]] = [
    {"key": "unread", "id": "in-case-you-missed", "eyebrow": "Catch up", "title": "In Case You Missed", "layout": "featured"},
    {"key": "week", "id": "new-this-week", "eyebrow": "This week", "title": "New This Week From Your Subscriptions", "layout": "grid"},
    {"key": "older", "id": "while-you-were-away", "eyebrow": "Backlog", "title": "While You Were Away", "layout": "featured"},
    {"key": "quick", "id": "quick-catch-up", "eyebrow": "Short on time", "title": "Quick Catch-Up", "layout": "grid"},
    {"key": "regulars", "id": "your-regulars", "eyebrow": "Your regulars", "title": "Back to the Channels You Watch Most", "layout": "featured"},
    {"key": "long", "id": "long-watch-catch-up", "eyebrow": "Deep watch", "title": "Long Watches You Missed", "layout": "compact"},
]


def _favorite_channel_keys(user_key: str, limit: int = 6) -> list[str]:
    """Channels the user actually watches, most-watched first."""
    counts: dict[str, int] = {}
    try:
        history = db.list_history_videos(60, user_key)
    except Exception:
        return []
    for video in history:
        key = str(video.get("youtube_channel_id") or video.get("channel_title") or "").strip().lower()
        if key:
            counts[key] = counts.get(key, 0) + 1
    return [key for key, _ in sorted(counts.items(), key=lambda item: -item[1])[:limit]]


def _catch_up_videos(shelf_key: str, pool: list[dict[str, Any]], user_key: str, limit: int = 8) -> list[dict[str, Any]]:
    unwatched = _without_shorts([row for row in pool if not row.get("percent_completed")])
    if shelf_key == "unread":
        return _without_shorts(_missed_subscription_videos(limit * 3, user_key, pool))[:limit]
    if shelf_key == "week":
        return [row for row in unwatched if 0 < _video_age_days(row) <= 7][:limit]
    if shelf_key == "older":
        return [row for row in unwatched if _video_age_days(row) >= 10][:limit]
    if shelf_key == "quick":
        return [row for row in unwatched if 90 < _video_duration(row) <= 600][:limit]
    if shelf_key == "long":
        return [row for row in unwatched if _video_duration(row) >= 1500][:limit]
    if shelf_key == "regulars":
        favorites = _favorite_channel_keys(user_key)
        if not favorites:
            return []
        ranked = sorted(
            (row for row in unwatched if str(row.get("youtube_channel_id") or row.get("channel_title") or "").strip().lower() in favorites),
            key=lambda row: favorites.index(str(row.get("youtube_channel_id") or row.get("channel_title") or "").strip().lower()),
        )
        return ranked[:limit]
    return []


def _feed_rotation_seed(user_key: str) -> int:
    """Stable per user and per day, so the shelf mix changes without shuffling mid-scroll."""
    day = datetime.now(timezone.utc).toordinal()
    return zlib.crc32(f"{user_key}:{day}".encode()) % 997


def _catch_up_section(pool: list[dict[str, Any]], seen: set[str], user_key: str, seed: int, page: int) -> dict[str, Any] | None:
    """Pick the next catch-up variant that actually has something to show."""
    if not pool:
        return None
    count = len(_CATCH_UP_SHELVES)
    start = (seed + page * 2) % count
    for step in range(count):
        shelf = _CATCH_UP_SHELVES[(start + step) % count]
        videos = _dedupe_section_videos(_catch_up_videos(shelf["key"], pool, user_key), seen, 8, spread_channels=False)
        if len(videos) >= 3:
            return {
                "id": f'{shelf["id"]}-{page}',
                "eyebrow": shelf["eyebrow"],
                "title": shelf["title"],
                "videos": videos,
                "layout": shelf["layout"],
                "kind": "catch-up",
            }
    return None


def _blend_subscription_videos(
    primary: list[dict[str, Any]],
    subscription_rows: list[dict[str, Any]],
    limit: int,
    every: int = 3,
) -> list[dict[str, Any]]:
    """Recommendation-led ordering with subscription uploads woven through it.

    The top of the home feed stays a recommendation run, but every `every`-th slot
    is reserved for a subscribed channel so following someone still means seeing
    them. Recommendation rows that are themselves subscription uploads simply fill
    those slots first, because duplicates are dropped.
    """
    subs: list[dict[str, Any]] = []
    sub_ids: set[str] = set()
    for row in subscription_rows:
        video_id = str(row.get("youtube_video_id") or "")
        if video_id and video_id not in sub_ids:
            sub_ids.add(video_id)
            subs.append(row)

    blended: list[dict[str, Any]] = []
    used: set[str] = set()
    channel_counts: dict[str, int] = {}
    primary_index = 0
    sub_index = 0
    channel_cap = 3

    def channel_of(row: dict[str, Any]) -> str:
        return str(row.get("youtube_channel_id") or row.get("channel_title") or "").strip().lower()

    def take(rows: list[dict[str, Any]], index: int, cap: bool) -> tuple[dict[str, Any] | None, int]:
        while index < len(rows):
            candidate = rows[index]
            index += 1
            video_id = str(candidate.get("youtube_video_id") or "")
            if not video_id or video_id in used:
                continue
            # The recommendation run is already diversified; injected uploads are not,
            # so cap them per channel or one busy creator takes over the top of the feed.
            channel = channel_of(candidate)
            if cap and channel and channel_counts.get(channel, 0) >= channel_cap:
                continue
            return candidate, index
        return None, index

    while len(blended) < limit:
        wants_subscription = every > 0 and (len(blended) + 1) % every == 0
        row = None
        if wants_subscription:
            row, sub_index = take(subs, sub_index, True)
        if row is None:
            row, primary_index = take(primary, primary_index, False)
        if row is None and not wants_subscription:
            row, sub_index = take(subs, sub_index, True)
        if row is None:
            break
        used.add(str(row.get("youtube_video_id") or ""))
        channel = channel_of(row)
        if channel:
            channel_counts[channel] = channel_counts.get(channel, 0) + 1
        blended.append(row)
    return blended


def _blend_cadence(preferences: dict[str, Any]) -> int:
    """How often a subscription upload is forced into a recommendation run."""
    every = 3 if preferences.get("prioritize_subscriptions", True) else 4
    try:
        discovery_level = float(preferences.get("discovery_level", 0.35))
    except (TypeError, ValueError):
        discovery_level = 0.35
    if discovery_level >= 0.7:
        every += 1
    return every


def _feed_section_html(
    section_id: str,
    eyebrow: str,
    title: str,
    videos: list[dict[str, Any]],
    settings: Any,
    layout: str = "grid",
    kind: str = "",
) -> str:
    if layout == "shorts":
        return _shorts_shelf(videos, section_id)
    if not videos:
        return ""
    kind_attr = f' data-section-kind="{escape(kind)}"' if kind else ""
    if layout == "featured":
        return _shelf_html(section_id, title, videos[:8], settings, eyebrow=eyebrow, kind=kind)
    heading_id = f"section-{escape(section_id)}"
    # No per-section Tune button: the tune chip in the filter row already opens this.
    eyebrow_html = f'<p class="section-eyebrow">{escape(eyebrow)}</p>' if eyebrow else ""
    if layout == "compact":
        body = '<div class="library-list">' + "".join(_media_row(video, settings) for video in videos) + '</div>'
    else:
        body = '<div class="video-grid">' + "".join(_home_video_card(video, index, settings) for index, video in enumerate(videos, start=1)) + '</div>'
    return f'''
      <section class="section-block feed-section" data-feed-section="{escape(section_id)}"{kind_attr} aria-labelledby="{heading_id}">
        <div class="section-heading">
          <div class="section-heading-copy">
            {eyebrow_html}
            <h2 id="{heading_id}">{escape(title)}</h2>
          </div>
        </div>
        {body}
      </section>
    '''


HOME_PAGE_SIZE = 30


def _home_feed_sections(page: int, feed_filter: str, settings: Any, user_key: str = "local") -> list[dict[str, Any]]:
    page = max(0, int(page or 0))
    filter_key = (feed_filter or "all").strip().lower() or "all"
    sections: list[dict[str, Any]] = []
    seen: set[str] = set()
    if filter_key != "all":
        videos = _dedupe_section_videos(
            db.list_recommended_videos(HOME_PAGE_SIZE, page * HOME_PAGE_SIZE, filter_key, user_key), seen, HOME_PAGE_SIZE
        )
        if videos:
            kind = "subscription" if filter_key in {"subscription", "subscriptions"} else "filtered"
            sections.append({"id": f"filter-{filter_key}-{page}", "eyebrow": "Filtered feed", "title": _feed_title_for_filter(filter_key), "videos": videos, "layout": "grid", "kind": kind})
        return sections

    preferences = db.get_feed_preferences(user_key)
    cadence = _blend_cadence(preferences)
    seed = _feed_rotation_seed(user_key)
    subscription_pool = _without_shorts(db.list_subscription_recommendations(160, user_key))
    show_shorts = not preferences.get("exclude_shorts", False)

    if page == 0:
        # Claimed before anything else: the recommendation run would otherwise pull
        # Shorts into the 16:9 grid and leave the Shorts shelf with nothing.
        shorts = _dedupe_section_videos(db.list_short_videos(14, user_key), seen, 14) if show_shorts else []

        # Recommendations are recommendations. Subscription uploads used to be woven into
        # this shelf every few slots, which meant the same handful of followed creators
        # filled the top of the page and genuinely recommended videos were pushed off it.
        # They now have their own shelf below and their own page, so this run is ranked
        # purely on merit — topics, watch time, and everything else the scorer knows.
        recommended = _without_shorts(db.list_recommended_videos(HOME_PAGE_SIZE + 24, 0, "recommended", user_key))
        top_videos = _dedupe_section_videos(recommended, seen, HOME_PAGE_SIZE)
        if top_videos:
            sections.append({"id": "recommended", "eyebrow": "For you", "title": "Recommended for you", "videos": top_videos, "layout": "grid", "kind": "recommended"})

        # Reserved before the remaining shelves so a catch-up variant is never
        # starved of videos, then placed further down the page below.
        catch_up = _catch_up_section(subscription_pool, seen, user_key, seed, page)

        continue_videos = _dedupe_section_videos(_without_shorts(db.list_recommended_videos(8, 0, "continue", user_key)), seen, 8, spread_channels=False)
        if continue_videos:
            sections.append({"id": "continue-watching", "eyebrow": "Resume", "title": "Continue watching", "videos": continue_videos, "layout": "grid", "kind": "continue"})
        sub_videos = _dedupe_section_videos(subscription_pool, seen, 12, spread_channels=False)
        if sub_videos:
            sections.append({"id": "new-subscriptions", "eyebrow": "Subscriptions", "title": "New from your subscriptions", "videos": sub_videos, "layout": "grid", "kind": "subscription"})
        if shorts:
            sections.append({"id": "shorts", "eyebrow": "Shorts", "title": "Shorts", "videos": shorts, "layout": "shorts", "kind": "shorts"})
        more = _dedupe_section_videos(
            _without_shorts(db.list_recommended_videos(HOME_PAGE_SIZE, HOME_PAGE_SIZE, "all", user_key)),
            seen,
            HOME_PAGE_SIZE,
        )
        if more:
            sections.append({"id": "recommended-more", "eyebrow": "Keep going", "title": "More recommendations", "videos": more, "layout": "grid", "kind": "recommended"})
        if catch_up:
            # Anywhere but the top: the first slot belongs to recommendations.
            sections.insert(min(len(sections), 1 + (seed % 3)), catch_up)
        return sections

    # Page 0 already served two runs, so deeper pages start past both of them.
    offset = (HOME_PAGE_SIZE * 2) + ((page - 1) * HOME_PAGE_SIZE)
    if page % 4 == 1:
        lane, title, eyebrow = "recommended", "More for you", "Personalized"
        section_id = f"channels-you-like-{page}"
    elif page % 4 == 2:
        lane, title, eyebrow = "recent", "Recently uploaded in your interests", "Fresh"
        section_id = f"recent-interests-{page}"
    elif page % 4 == 3:
        lane, title, eyebrow = "long-form", "Long-form picks", "Deep watch"
        section_id = f"long-form-{page}"
    else:
        lane, title, eyebrow = "recommended", "Personalized discovery", "Explore"
        section_id = f"discovery-{page}"

    videos = db.list_recommended_videos(HOME_PAGE_SIZE, offset, lane, user_key)
    if len(videos) < 8 and lane != "all":
        # A narrow lane runs dry long before the library does. Top it up rather than
        # returning an empty page, which would stop infinite scroll for good.
        videos = videos + db.list_recommended_videos(HOME_PAGE_SIZE, offset, "recommended", user_key)
        title, eyebrow = "More recommendations", "Keep going"
        section_id = f"more-{page}"

    # Deeper pages are recommendations too — subscriptions reach the feed through their
    # own shelf and the catch-up shelf below, not by being injected into every grid.
    videos = _dedupe_section_videos(_without_shorts(videos), seen, HOME_PAGE_SIZE)
    if videos:
        sections.append({"id": section_id, "eyebrow": eyebrow, "title": title, "videos": videos, "layout": "grid", "kind": "recommended"})

    # A catch-up shelf turns up every few pages, above or below that page's grid,
    # so it reads as part of the feed instead of a fixture pinned to the top.
    if (page + seed) % 3 == 0:
        catch_up = _catch_up_section(subscription_pool, seen, user_key, seed, page)
        if catch_up:
            sections.insert(0 if (page + seed) % 2 == 0 else len(sections), catch_up)

    # Shorts keep reappearing as you scroll, the same way they do on the Shorts row.
    if show_shorts and sections and page % 3 == 2:
        shorts = _dedupe_section_videos(db.list_short_videos(14, user_key, offset=14 * (page // 3 + 1)), seen, 14)
        if shorts:
            sections.append({"id": f"shorts-{page}", "eyebrow": "Shorts", "title": "Shorts", "videos": shorts, "layout": "shorts", "kind": "shorts"})
    return sections


def _home_feed_sections_html(page: int, feed_filter: str, settings: Any, user_key: str = "local") -> str:
    return "".join(
        _feed_section_html(
            section["id"],
            section["eyebrow"],
            section["title"],
            section["videos"],
            settings,
            section.get("layout", "grid"),
            section.get("kind", ""),
        )
        for section in _home_feed_sections(page, feed_filter, settings, user_key)
    )


def _home_styles() -> str:
    return '''
      :root {
        color-scheme: dark;
        font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Inter", "Segoe UI", Roboto, system-ui, sans-serif;
        --bg: #0a0a0b;
        --surface: #121214;
        --surface-2: #1a1a1d;
        --surface-3: #232327;
        --menu: #151517;
        --border: rgba(255,255,255,.09);
        --border-soft: rgba(255,255,255,.055);
        --text: #ededef;
        --muted: #8b8b93;
        --muted-2: #6a6a72;
        --accent: #e5484d;
        --accent-soft: rgba(229,72,77,.12);
        --sidebar: 244px;
        --header: 56px;
        --focus: 0 0 0 2px rgba(255,255,255,.3);
        --r-thumb: 12px;
        --r-panel: 14px;
        --r-menu: 14px;
        --r-control: 8px;
        --shadow: 0 12px 32px rgba(0,0,0,.55);
      }
      * { box-sizing: border-box; }
      html { background: var(--bg); }
      body { margin: 0; min-width: 320px; background: var(--bg); color: var(--text); -webkit-font-smoothing: antialiased; -moz-osx-font-smoothing: grayscale; letter-spacing: -.006em; }
      a { color: inherit; text-decoration: none; }
      button, input, select { font: inherit; letter-spacing: inherit; }
      button { color: inherit; }
      /* Scoped to page content: the app shell, dialogs and menus style their own
         headings explicitly, so a global rule only leaked letter-spacing into them. */
      main h1, main h2, main h3 { letter-spacing: -.018em; font-weight: 600; }
      .icon { width: 20px; height: 20px; fill: none; stroke: currentColor; stroke-width: 1.75; stroke-linecap: round; stroke-linejoin: round; flex: 0 0 auto; }
      :focus-visible { outline: none; box-shadow: var(--focus); }
      /* Keyboard-only way past the header/sidebar/nav. Hidden until focused. */
      .skip-link { position: fixed; top: -48px; left: 12px; z-index: 200; border-radius: 999px; background: var(--surface-2); color: var(--text); border: 1px solid var(--border); padding: 8px 14px; font-size: 13px; transition: top .12s ease; }
      .skip-link:focus-visible { top: 8px; box-shadow: var(--focus); }
      /* The skip-link target itself takes programmatic focus; a ring around the whole
         page would be noise, so only the link shows one. */
      #main:focus { box-shadow: none; }
      ::-webkit-scrollbar { width: 10px; height: 10px; }
      ::-webkit-scrollbar-thumb { background: #2a2a2e; border: 3px solid var(--bg); border-radius: 999px; }
      ::-webkit-scrollbar-thumb:hover { background: #35353a; }

      /* ---------- top bar ---------- */
      .app-header { position: fixed; top: 0; left: 0; right: 0; z-index: 60; height: var(--header); background: rgba(10,10,11,.86); backdrop-filter: saturate(160%) blur(14px); border-bottom: 1px solid var(--border-soft); display: grid; grid-template-columns: minmax(150px, 1fr) minmax(0, 560px) minmax(150px, 1fr); align-items: center; gap: 16px; padding: 0 16px; }
      .header-left { display: flex; align-items: center; gap: 6px; min-width: 0; }
      .brand { display: inline-flex; align-items: center; gap: 9px; padding: 0 6px; font-size: 16px; letter-spacing: -.025em; color: var(--text); }
      .brand-mark { display: grid; place-items: center; width: 26px; height: 26px; }
      .brand-mark img { width: 26px; height: 26px; display: block; object-fit: contain; }
      /* "Open" light, "Feed" solid: the weight break carries the wordmark, so the tile
         does not have to hold a word at 9px to say what this is. */
      .brand-word { font-weight: 400; color: var(--muted); }
      .brand-word b { font-weight: 680; color: var(--text); }
      .brand:hover .brand-word { color: var(--text); }
      .header-center { display: flex; align-items: center; gap: 8px; justify-content: center; min-width: 0; }
      .search-form { flex: 1; min-width: 0; height: 36px; display: flex; align-items: center; border: 1px solid var(--border); border-radius: 999px; background: var(--surface); overflow: hidden; transition: border-color .12s ease, background .12s ease; }
      .search-form:hover { background: var(--surface-2); }
      .search-form:focus-within { border-color: rgba(255,255,255,.22); background: var(--surface-2); }
      .search-form input { flex: 1; min-width: 0; height: 100%; border: 0; background: transparent; color: var(--text); padding: 0 16px; outline: none; font-size: 13.5px; }
      .search-form input::placeholder { color: var(--muted-2); }
      /* Outset ring would clip under the form's overflow:hidden, so the input gets an
         inset one; the form border still lights up via :focus-within above. */
      .search-form input:focus-visible { box-shadow: inset 0 0 0 2px rgba(255,255,255,.3); border-radius: 999px; }
      .search-form button { width: 52px; height: 100%; border: 0; border-left: 1px solid var(--border-soft); background: var(--surface-2); display: grid; place-items: center; cursor: pointer; color: var(--muted); }
      .search-form button:hover { color: var(--text); background: var(--surface-3); }
      .header-actions { display: flex; justify-content: flex-end; align-items: center; gap: 6px; }
      /* The bell is a 34px hit area around a 20px glyph; the avatar is a bare 28px circle
         with no padding of its own. Matching gaps therefore look uneven, and the avatar
         ends up almost touching the bell — hence the extra room on this one. */
      .header-actions .profile-menu { margin-left: 6px; }
      .icon-button { width: 34px; height: 34px; border: 0; border-radius: 999px; background: transparent; display: grid; place-items: center; cursor: pointer; color: var(--muted); transition: background .12s ease, color .12s ease; }
      .icon-button:hover { background: var(--surface-2); color: var(--text); }
      .compact-only { display: none; }
      .profile-dot { display: grid; place-items: center; width: 28px; height: 28px; border-radius: 999px; background: var(--surface-3); border: 1px solid var(--border); color: var(--text); font-size: 10px; font-weight: 600; letter-spacing: .02em; }

      /* ---------- sidebar ---------- */
      .desktop-sidebar { position: fixed; top: var(--header); bottom: 0; left: 0; z-index: 50; width: var(--sidebar); background: var(--bg); border-right: 1px solid var(--border-soft); padding: 10px 10px 24px; overflow-y: auto; overscroll-behavior: contain; scrollbar-width: thin; }
      .desktop-sidebar::-webkit-scrollbar { width: 6px; }
      .desktop-sidebar::-webkit-scrollbar-thumb { border-width: 2px; }
      .nav-group { display: grid; align-content: start; gap: 1px; padding: 10px 0; border-bottom: 1px solid var(--border-soft); }
      .nav-group:first-child { padding-top: 0; }
      .nav-group:last-child { border-bottom: 0; }
      .nav-group-title { margin: 6px 0 6px; padding: 0 12px; font-size: 13px; font-weight: 600; letter-spacing: -.01em; color: var(--text); }
      .nav-more { border: 0; background: none; width: 100%; cursor: pointer; color: var(--text); }
      .nav-more .icon { transition: transform .18s ease; }
      .nav-more[aria-expanded="true"] .icon { transform: rotate(180deg); }
      .nav-heading { margin: 2px 0 6px; padding: 0 12px; color: var(--text); font-size: 14px; font-weight: 600; letter-spacing: -.01em; }
      .nav-item { position: relative; min-height: 38px; border-radius: 10px; display: flex; align-items: center; gap: 16px; padding: 0 12px; color: var(--text); font-size: 13.5px; transition: background .12s ease, color .12s ease; }
      .nav-item:hover { background: var(--surface-2); }
      .nav-item.active { background: var(--surface-3); font-weight: 500; }
      .nav-item.active .icon { color: var(--accent); }
      .nav-item .icon { width: 20px; height: 20px; color: var(--muted); }
      .nav-channel { gap: 12px; min-height: 38px; }
      .nav-channel span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .nav-channel .avatar { width: 24px; height: 24px; font-size: 9.5px; }
      .nav-dot { width: 5px; height: 5px; margin-left: auto; border-radius: 999px; background: var(--accent); flex: 0 0 auto; }
      .sidebar-meta { color: var(--muted); font-size: 11.5px; line-height: 1.6; padding: 14px 10px 0; }
      .sidebar-meta p { margin: 0 0 4px; }
      .sidebar-meta .source-offer { margin-top: 10px; }
      .sidebar-meta .source-offer a { color: var(--muted); text-decoration: none; border-bottom: 1px solid transparent; }
      .sidebar-meta .source-offer a:hover { color: var(--text); border-bottom-color: currentColor; }

      /* ---------- page frame ---------- */
      .home-main { margin-left: var(--sidebar); padding: calc(var(--header) + 20px) 32px 72px; max-width: 2200px; }
      /* The rail slides rather than disappearing, and only animates after first paint
         so a page that starts collapsed does not play the transition on load. */
      body.nav-ready .desktop-sidebar { transition: transform .18s cubic-bezier(.2,.8,.3,1), opacity .16s ease; }
      body.nav-ready .home-main { transition: margin-left .18s cubic-bezier(.2,.8,.3,1); }
      body.nav-collapsed .desktop-sidebar { transform: translateX(-100%); opacity: 0; pointer-events: none; }
      body.nav-collapsed .home-main { margin-left: 0; }
      /* On watch/Shorts the rail floats over the page instead of pushing it. */
      body.nav-overlay .home-main { margin-left: 0; }
      body.nav-overlay .desktop-sidebar { box-shadow: var(--shadow); background: var(--surface); }
      .nav-scrim { position: fixed; inset: var(--header) 0 0; z-index: 45; background: rgba(0,0,0,.5);
        opacity: 0; pointer-events: none; transition: opacity .18s ease; }
      body.nav-overlay:not(.nav-collapsed) .nav-scrim { opacity: 1; pointer-events: auto; }
      .nav-toggle .icon { transition: opacity .18s ease; }
      body.nav-overlay:not(.nav-collapsed) .nav-toggle { background: var(--surface-3); color: var(--text); }
      @media (prefers-reduced-motion: reduce) {
        body.nav-ready .desktop-sidebar, body.nav-ready .home-main, .nav-scrim, .nav-toggle .icon { transition: none; }
      }
      .topic-wrap { position: sticky; top: var(--header); z-index: 40; margin: -20px -32px 20px; padding: 12px 32px; background: rgba(10,10,11,.9); backdrop-filter: blur(12px); border-bottom: 1px solid var(--border-soft); overflow-x: auto; scrollbar-width: none; }
      .topic-wrap::-webkit-scrollbar, .creator-strip::-webkit-scrollbar, .shelf-track::-webkit-scrollbar, .page-tabs::-webkit-scrollbar { display: none; }
      .topic-row { display: flex; align-items: center; gap: 8px; width: max-content; min-width: 100%; }
      .topic-chip { height: 34px; border: 1px solid transparent; border-radius: 999px; background: var(--surface-2); color: var(--text); padding: 0 14px; white-space: nowrap; cursor: pointer; display: inline-flex; align-items: center; justify-content: center; gap: 6px; font-size: 13.5px; line-height: 1; transition: background .12s ease, color .12s ease; }
      .topic-chip:hover { background: var(--surface-3); }
      .topic-chip.active { background: #f1f1f3; color: #0a0a0b; font-weight: 500; }
      .topic-action { flex: 0 0 auto; width: 34px; padding: 0; color: var(--text); }
      .topic-action .icon { width: 16px; height: 16px; }
      .topic-divider { width: 1px; height: 18px; background: var(--border); flex: 0 0 auto; margin: 0 4px; }
      .creator-strip { display: none; overflow-x: auto; gap: 14px; padding: 4px 0 14px; }
      .creator-pill { display: grid; justify-items: center; gap: 6px; min-width: 62px; color: var(--muted); font-size: 11px; text-align: center; }
      .creator-pill span:last-child { max-width: 62px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .avatar { display: grid; place-items: center; width: 32px; height: 32px; border-radius: 999px; color: #d6d6da; background: hsl(var(--avatar-hue, 240) 8% 20%); border: 1px solid var(--border-soft); font-weight: 600; font-size: 11px; letter-spacing: .02em; flex: 0 0 auto; }
      .creator-pill .avatar { width: 46px; height: 46px; font-size: 14px; }
      .avatar { position: relative; overflow: hidden; }
      .avatar-initials { display: block; line-height: 1; }
      .avatar img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; display: block; }
      .avatar.has-image { background: var(--surface-2); }
      .channel-banner.has-image { padding: 0; background: var(--surface); }
      .channel-banner.has-image img { width: 100%; height: 100%; object-fit: cover; object-position: center center; display: block; }

      /* ---------- sections ---------- */
      .section-block { margin-top: 40px; }
      .section-block:first-child { margin-top: 0; }
      .section-heading { display: flex; align-items: flex-end; justify-content: space-between; gap: 16px; margin-bottom: 18px; }
      .section-heading h2 { margin: 0; font-size: 17px; font-weight: 600; line-height: 1.3; letter-spacing: -.014em; }
      .section-heading-copy { display: grid; gap: 3px; min-width: 0; }
      .section-eyebrow { margin: 0; font-size: 11px; font-weight: 600; letter-spacing: .07em; text-transform: uppercase; color: var(--muted-2); }
      [data-section-kind="catch-up"] .section-eyebrow { color: var(--accent); }
      .eyebrow { display: none; }
      .is-removing { opacity: 0; transform: scale(.97); transition: opacity .2s ease, transform .2s ease; pointer-events: none; }
      @media (prefers-reduced-motion: reduce) { .is-removing { transition: none; } }
      .text-action { height: 32px; border: 0; border-radius: 999px; background: var(--surface-2); color: var(--text); display: inline-flex; align-items: center; gap: 7px; padding: 0 14px; cursor: pointer; font-size: 13px; font-weight: 500; transition: background .12s ease, color .12s ease; }
      .text-action:hover { background: var(--surface-3); }
      .text-action .icon { width: 15px; height: 15px; }
      .primary-action { min-height: 32px; border: 0; border-radius: 999px; background: #f1f1f3; color: #0a0a0b; display: inline-flex; align-items: center; gap: 7px; padding: 0 16px; cursor: pointer; font-size: 13px; font-weight: 600; }
      .primary-action:hover { background: #fff; }

      /* ---------- shelf ---------- */
      .shelf { position: relative; border: 1px solid var(--border-soft); border-radius: 16px; background: var(--surface); padding: 18px 20px 20px; }
      .shelf-head { display: flex; align-items: flex-end; justify-content: space-between; gap: 12px; margin-bottom: 16px; }
      .shelf-head h2 { margin: 0; font-size: 16px; font-weight: 600; letter-spacing: -.012em; }
      .shelf-viewport { position: relative; }
      .shelf-track { display: grid; grid-auto-flow: column; grid-auto-columns: calc((100% - 3 * 16px) / 4); gap: 16px; overflow-x: auto; scroll-behavior: smooth; scroll-snap-type: x mandatory; scrollbar-width: none; }
      .shelf-track > * { scroll-snap-align: start; }
      .shelf-nav { position: absolute; top: 33%; z-index: 3; width: 34px; height: 34px; transform: translateY(-50%); border: 1px solid var(--border); border-radius: 999px; background: rgba(18,18,20,.94); color: var(--text); display: grid; place-items: center; cursor: pointer; backdrop-filter: blur(6px); }
      .shelf-nav:hover { background: var(--surface-3); }
      .shelf-nav[hidden] { display: none; }
      .shelf-nav.prev { left: -14px; }
      .shelf-nav.next { right: -14px; }
      .shelf-nav .icon { width: 15px; height: 15px; }

      /* ---------- video cards ---------- */
      /* Three across at most. auto-fill packs in as many 288px columns as fit, which on
         a wide window is four and makes each card small enough that titles truncate.
         min() caps the count while still collapsing to 2 and 1 on narrower screens. */
      .video-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(max(288px, (100% - 40px) / 3), 1fr));
        gap: 34px 20px; align-items: start; }
      .video-card { min-width: 0; color: var(--text); }
      .thumb { position: relative; display: grid; place-items: center; aspect-ratio: 16 / 9; overflow: hidden; border-radius: var(--r-thumb); background: #000; border: 1px solid var(--border-soft); color: #fff; }
      .thumb img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; }
      /* Hover preview player: covers the thumbnail once it can play, never intercepts
         clicks (they fall through to the watch link), and fades in so a slow first
         paint does not flash. The duration badge hides underneath while it plays. */
      .thumb .card-preview { position: absolute; inset: 0; z-index: 1; width: 100%; height: 100%; object-fit: cover; background: #000; pointer-events: none; opacity: 0; transition: opacity .18s ease; }
      .is-previewing .thumb .card-preview { opacity: 1; }
      .is-previewing .thumb .duration { opacity: 0; }
      .thumb .preview-progress { position: absolute; left: 0; right: 0; bottom: 0; z-index: 2; height: 2px; background: rgba(255,255,255,.28); opacity: 0; transition: opacity .18s ease; pointer-events: none; }
      .is-previewing .thumb .preview-progress { opacity: 1; }
      .thumb .preview-progress i { display: block; height: 100%; width: 0; background: #f03; }
      .thumb-fallback { display: grid; place-items: center; width: 42px; height: 26px; border-radius: 6px; background: var(--surface-3); color: var(--muted); font-weight: 600; font-size: 10px; }
      .duration { position: absolute; right: 6px; bottom: 6px; z-index: 2; border-radius: 4px; background: rgba(0,0,0,.82); color: #fff; font-size: 11px; font-weight: 500; font-variant-numeric: tabular-nums; line-height: 1; padding: 3px 5px; }
      .progress-rail { position: absolute; left: 0; right: 0; bottom: 0; height: 3px; background: rgba(255,255,255,.18); z-index: 2; }
      .progress-rail span { display: block; height: 100%; background: var(--accent); }
      .video-info { display: grid; grid-template-columns: 32px minmax(0, 1fr); gap: 11px; padding: 11px 0 0; }
      .card-copy { min-width: 0; }
      .title-row { display: grid; grid-template-columns: minmax(0, 1fr) 22px; gap: 8px; align-items: start; }
      .video-card h3 { margin: 0; font-size: 14.5px; line-height: 1.4; font-weight: 550; letter-spacing: -.008em; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
      .channel-line, .metadata { margin: 5px 0 0; color: var(--muted); font-size: 13px; line-height: 1.35; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .channel-line { display: flex; align-items: center; gap: 4px; }
      .channel-line a:hover { color: var(--text); }
      .verified { width: 12px; height: 12px; flex: 0 0 auto; color: var(--muted-2); }
      .metadata { margin-top: 2px; color: var(--muted); font-variant-numeric: tabular-nums; }
      .card-badges { display: none; }
      .video-card-featured h3 { font-size: 14px; }
      .video-card-mini { display: grid; grid-template-columns: minmax(110px, 160px) minmax(0, 1fr) 22px; gap: 10px; align-items: start; }
      .video-card-mini .thumb { border-radius: 8px; }
      .video-card-mini h3 { font-size: 13px; }
      .video-card-mini p { margin: 5px 0 0; color: var(--muted); font-size: 12px; line-height: 1.3; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

      /* ---------- menus ---------- */
      .yt-menu { position: relative; min-width: 0; }
      .yt-menu > summary { list-style: none; cursor: pointer; }
      .yt-menu > summary::-webkit-details-marker { display: none; }
      .yt-menu > summary:focus-visible { box-shadow: var(--focus); }
      .card-menu { justify-self: end; }
      /* The layout box stays 22x26 so card grids keep their column: min-width/height
         (this stylesheet is border-box, so padding alone cannot grow the box) reach
         32x32, and the negative margin pulls the footprint back to 22x26. */
      .menu-trigger { width: 22px; height: 26px; min-width: 32px; min-height: 32px; margin: -3px -5px; border-radius: 6px; display: grid; place-items: center; color: var(--muted); }
      .menu-trigger .icon { width: 16px; height: 16px; }
      .menu-trigger:hover, .yt-menu[open] > .menu-trigger, .header-menu[open] > summary, .watch-menu[open] > summary { background: var(--surface-3); color: var(--text); }
      .header-menu { display: grid; place-items: center; }
      .header-menu > summary.icon-button, .profile-menu > summary.profile-dot { display: grid; place-items: center; }
      .profile-menu > summary.profile-dot { cursor: pointer; }
      .watch-menu { display: inline-flex; }
      .watch-menu .menu-panel { left: 0; right: auto; top: 38px; }
      .yt-menu.drop-up .menu-panel { top: auto; bottom: 34px; }
      .watch-menu.drop-up .menu-panel { top: auto; bottom: 38px; }
      .menu-panel { position: absolute; right: 0; top: 32px; z-index: 90; width: min(268px, calc(100vw - 24px)); max-height: min(var(--menu-max-height, 620px), calc(100vh - 90px)); overflow: auto; padding: 5px; border: 1px solid var(--border); border-radius: var(--r-menu); background: var(--menu); box-shadow: var(--shadow); display: grid; gap: 1px; color: var(--text); scrollbar-width: thin; }
      .menu-panel-wide { width: min(320px, calc(100vw - 24px)); }
      .account-panel { width: min(268px, calc(100vw - 24px)); }
      .menu-heading { padding: 8px 10px; display: flex; align-items: center; justify-content: space-between; gap: 12px; border-bottom: 1px solid var(--border-soft); margin-bottom: 4px; }
      .menu-heading span { font-size: 12.5px; font-weight: 600; color: var(--text); }
      .menu-heading small { color: var(--muted-2); font-size: 11px; }
      .menu-row { min-height: 36px; width: 100%; border: 0; border-radius: 8px; background: transparent; color: var(--text); display: grid; grid-template-columns: 18px minmax(0, 1fr) auto; align-items: center; gap: 11px; padding: 0 10px; text-align: left; cursor: pointer; font-size: 13px; }
      .menu-row:hover, .menu-row:focus-visible { background: var(--surface-3); }
      .menu-row.danger .menu-icon, .menu-row.danger .menu-label { color: #f2686c; }
      .menu-row.disabled { cursor: default; color: var(--muted-2); }
      .menu-row.disabled:hover { background: transparent; }
      .menu-icon { display: grid; place-items: center; color: var(--muted); }
      .menu-icon .icon { width: 16px; height: 16px; }
      .menu-copy { min-width: 0; display: block; }
      .menu-label { display: block; line-height: 1.35; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .menu-sub { display: none; }
      .menu-row.stacked { min-height: 44px; padding: 6px 10px; }
      .menu-row.stacked .menu-sub { display: block; color: var(--muted-2); font-size: 11.5px; line-height: 1.3; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .menu-trailing { color: var(--muted-2); font-size: 11.5px; }
      .menu-separator { height: 1px; margin: 5px 0; background: var(--border-soft); }
      .account-summary { display: flex; align-items: center; gap: 10px; padding: 8px 10px; }
      .account-summary strong { display: block; font-size: 13px; font-weight: 600; }
      .account-summary small { color: var(--muted-2); font-size: 11.5px; }
      .notifications-panel .menu-row { min-height: 46px; }

      /* ---------- feed loading ---------- */
      .empty-panel, .empty-copy { color: var(--muted); }
      .feed-loading, .feed-retry { min-height: 48px; margin: 32px auto 0; color: var(--muted); display: flex; align-items: center; justify-content: center; gap: 10px; font-size: 12.5px; }
      .feed-loading[hidden], .feed-retry[hidden] { display: none; }
      .feed-loading span:first-child { width: 14px; height: 14px; border-radius: 999px; border: 1.5px solid rgba(255,255,255,.14); border-top-color: var(--muted); animation: spin .8s linear infinite; }
      .feed-retry button { height: 30px; border: 1px solid var(--border); border-radius: var(--r-control); background: transparent; color: var(--text); padding: 0 12px; cursor: pointer; }
      .feed-sentinel { height: 1px; }
      @keyframes spin { to { transform: rotate(360deg); } }
      .feedback-toast { position: fixed; left: 50%; bottom: 24px; z-index: 100; transform: translateX(-50%) translateY(12px); opacity: 0; pointer-events: none; border: 1px solid var(--border); border-radius: 999px; background: var(--menu); color: var(--text); padding: 9px 10px 9px 14px; font-size: 12.5px; transition: opacity .16s ease, transform .16s ease; display: inline-flex; align-items: center; gap: 14px; box-shadow: var(--shadow); }
      .feedback-toast.show { opacity: 1; transform: translateX(-50%) translateY(0); pointer-events: auto; }
      .feedback-toast button { height: 26px; border: 0; border-radius: 6px; background: transparent; color: var(--text); font-weight: 600; padding: 0 8px; cursor: pointer; }
      .feedback-toast button:hover { background: var(--surface-3); }

      /* ---------- dialogs ---------- */
      .feed-dialog { width: min(680px, calc(100vw - 32px)); border: 1px solid var(--border); border-radius: 18px; background: var(--surface); color: var(--text); padding: 0; box-shadow: 0 24px 64px rgba(0,0,0,.7); }
      .feed-dialog::backdrop { background: rgba(0,0,0,.62); backdrop-filter: blur(2px); }
      .dialog-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 18px; padding: 18px 20px; border-bottom: 1px solid var(--border-soft); }
      .dialog-head h2 { margin: 0; font-size: 15px; font-weight: 600; }
      .dialog-head p { margin: 6px 0 0; color: var(--muted); font-size: 12.5px; line-height: 1.5; }
      .dialog-body { padding: 18px 20px; display: grid; gap: 14px; }
      .dialog-grid { grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); align-items: start; }
      .dialog-card { border: 1px solid var(--border-soft); border-radius: 10px; background: var(--surface-2); padding: 14px; }
      .dialog-card:nth-child(3) { grid-column: 1 / -1; }
      .range-row, .text-field { display: grid; gap: 8px; color: var(--text); font-size: 12.5px; }
      .range-row span { display: grid; gap: 4px; }
      .range-row strong { font-size: 13px; font-weight: 550; }
      .range-row small, .dialog-note { color: var(--muted); font-size: 11.5px; line-height: 1.5; }
      .range-row input[type=range] { width: 100%; accent-color: var(--accent); }
      .text-field input, .field-input { width: 100%; min-height: 36px; border: 1px solid var(--border); border-radius: var(--r-control); background: var(--bg); color: var(--text); padding: 0 11px; outline: none; font-size: 13px; }
      .text-field input:focus-visible, .field-input:focus-visible { box-shadow: var(--focus); }
      .signal-list { display: flex; flex-wrap: wrap; gap: 6px; }
      .signal-list span { min-height: 26px; display: inline-flex; align-items: center; border: 1px solid var(--border); border-radius: 6px; background: transparent; color: var(--muted); padding: 0 9px; font-size: 11.5px; }
      /* ---------- settings dialog ---------- */
      /* Wider and taller: the panes now carry real content — the source ladder, the
   privacy note, and the Takeout importer with its step-by-step instructions —
   and 860px left all of it scrolling in a narrow column. */
      .settings-dialog { width: min(1180px, calc(100vw - 48px)); height: min(760px, calc(100vh - 64px)); overflow: hidden; }
      .settings-dialog .dialog-body { max-height: none; height: 100%; }
      .settings-dialog[open] { animation: tune-in .22s cubic-bezier(.2,.9,.3,1); }
      .settings-dialog::backdrop { background: rgba(0,0,0,.66); backdrop-filter: blur(6px); }
      .settings-shell { display: grid; grid-template-columns: 232px minmax(0, 1fr); height: 100%; min-height: 0; }
      .settings-nav { display: flex; flex-direction: column; gap: 2px; padding: 18px 12px 12px; border-right: 1px solid var(--border-soft);
        background: linear-gradient(180deg, rgba(255,255,255,.03), transparent 40%), var(--surface-2); }
      .settings-nav-head { padding: 0 10px 14px; }
      .settings-nav-head h2 { margin: 4px 0 0; font-size: 17px; font-weight: 600; letter-spacing: -.018em; }
      .settings-nav-item { display: flex; align-items: center; gap: 11px; min-height: 36px; padding: 0 10px; border: 0; border-radius: 9px;
        background: transparent; color: var(--muted); font-size: 13px; text-align: left; cursor: pointer; transition: background .12s ease, color .12s ease; }
      .settings-nav-item .icon { width: 17px; height: 17px; }
      .settings-nav-item:hover { background: var(--surface-3); color: var(--text); }
      .settings-nav-item.active { background: var(--surface-3); color: var(--text); font-weight: 550; }
      .settings-nav-item.active .icon { color: var(--accent); }
      .settings-nav-foot { margin-top: auto; display: flex; align-items: center; gap: 10px; padding: 12px 10px 2px; border-top: 1px solid var(--border-soft); min-width: 0; }
      .settings-nav-foot strong { display: block; font-size: 12.5px; font-weight: 550; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .settings-nav-foot small { display: block; color: var(--muted-2); font-size: 11px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .settings-nav-foot > div { min-width: 0; }
      .settings-content { position: relative; padding: 22px 24px 24px; overflow-y: auto; }
      .settings-close { position: absolute; top: 14px; right: 14px; }
      .settings-pane { display: none; gap: 14px; }
      .settings-pane.active { display: grid; align-content: start; }
      .settings-pane-head { padding-right: 44px; }
      .settings-pane-head h3 { margin: 0; font-size: 16px; font-weight: 600; letter-spacing: -.014em; }
      .settings-pane-head p { margin: 6px 0 0; color: var(--muted); font-size: 12.5px; line-height: 1.55; max-width: 56ch; }
      .settings-pane-actions { display: flex; align-items: center; flex-wrap: wrap; gap: 10px; }
      .settings-identity { display: flex; align-items: flex-start; gap: 14px; padding: 14px; border: 1px solid var(--border-soft); border-radius: 12px; background: var(--surface-2); }
      .settings-identity strong { display: block; font-size: 14px; }
      .settings-identity small { display: block; color: var(--muted); font-size: 12px; margin-bottom: 10px; }
      .profile-dot.lg { width: 40px; height: 40px; font-size: 13px; }
      .field-label { margin: 0; font-size: 11px; font-weight: 600; letter-spacing: .07em; text-transform: uppercase; color: var(--muted-2); }

      /* draggable playback source list */
      .source-list { display: grid; gap: 8px; }
      .source-row { display: grid; grid-template-columns: 22px 10px minmax(0, 1fr) auto auto; align-items: center; gap: 11px;
        min-height: 52px; padding: 9px 12px 9px 6px; border: 1px solid var(--border-soft); border-radius: 12px; background: var(--surface-2);
        transition: border-color .15s ease, background .15s ease, transform .15s ease, opacity .15s ease; }
      .source-row:hover { border-color: rgba(255,255,255,.16); }
      .source-row.dragging { opacity: .55; transform: scale(.99); border-color: var(--accent); }
      .source-row.drop-target { border-color: var(--accent); box-shadow: 0 -2px 0 -1px var(--accent) inset; }
      .source-row.is-off { opacity: .5; }
      .source-row .grip { display: grid; place-items: center; height: 28px; border: 0; background: transparent; color: var(--muted-2); cursor: grab; border-radius: 6px; }
      .source-row .grip:hover { color: var(--text); background: var(--surface-3); }
      .source-row .grip:active { cursor: grabbing; }
      .source-row .grip svg { width: 14px; height: 14px; stroke: currentColor; fill: none; stroke-width: 2; stroke-linecap: round; }
      .source-row .dot { width: 8px; height: 8px; border-radius: 999px; background: var(--muted-2); }
      .source-row.healthy .dot { background: #3fb950; box-shadow: 0 0 8px rgba(63,185,80,.45); }
      .source-row.cooling .dot { background: #d29922; box-shadow: 0 0 8px rgba(210,153,34,.4); }
      .source-row.failing .dot { background: var(--accent); box-shadow: 0 0 8px rgba(229,72,77,.4); }
      .source-copy { min-width: 0; }
      .source-copy strong { display: block; font-size: 13px; font-weight: 550; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .source-copy small { display: block; color: var(--muted-2); font-size: 11.5px; margin-top: 2px; }
      .source-rank { color: var(--muted-2); font-variant-numeric: tabular-nums; font-size: 11px; }
      .source-move { display: grid; gap: 2px; }
      .source-move button { width: 20px; height: 15px; border: 0; border-radius: 4px; background: transparent; color: var(--muted-2); cursor: pointer; font-size: 9px; line-height: 1; }
      .source-move button:hover:not(:disabled) { background: var(--surface-3); color: var(--text); }
      .source-move button:disabled { opacity: .3; cursor: default; }
      .instance-editor { display: grid; gap: 10px; padding: 14px; border: 1px solid var(--border-soft); border-radius: 12px; background: var(--surface-2); }
      .instance-editor[hidden] { display: none; }
      .instance-add { display: flex; gap: 8px; }
      .instance-add .field-input { flex: 1; min-width: 0; }
      .diagnosis { display: grid; gap: 10px; padding: 14px; border: 1px solid var(--border-soft); border-radius: 12px; background: var(--bg); }
      .diagnosis[hidden] { display: none; }
      .diagnosis h4 { margin: 0; font-size: 13px; font-weight: 600; }
      .diagnosis p { margin: 0; color: var(--muted); font-size: 12.5px; line-height: 1.55; }
      .diagnosis .chain { display: flex; flex-wrap: wrap; gap: 6px; }
      .diagnosis .hop { display: inline-flex; align-items: center; gap: 6px; padding: 3px 9px; border-radius: 999px;
        border: 1px solid var(--border); font-size: 11px; color: var(--muted); }
      .diagnosis .hop::before { content: ''; width: 6px; height: 6px; border-radius: 999px; background: currentColor; }
      .diagnosis .hop.ok { color: #3fb950; }
      .diagnosis .hop.bad { color: var(--accent); border-color: rgba(229,72,77,.4); }
      .diagnosis ul { margin: 0; padding: 0; list-style: none; display: grid; gap: 6px; }
      .diagnosis li { position: relative; padding-left: 18px; color: var(--text); font-size: 12px; line-height: 1.5; }
      .diagnosis li::before { content: '\\2192'; position: absolute; left: 0; color: var(--muted-2); }
      @media (max-width: 720px) {
        .settings-shell { grid-template-columns: 1fr; max-height: 86vh; }
        .settings-nav { flex-direction: row; overflow-x: auto; gap: 6px; padding: 10px; border-right: 0; border-bottom: 1px solid var(--border-soft); scrollbar-width: none; }
        .settings-nav-head, .settings-nav-foot { display: none; }
        .settings-nav-item { flex: 0 0 auto; }
        .settings-content { padding: 18px 16px 20px; }
      }
      .settings-grid { grid-template-columns: minmax(0, 1.1fr) minmax(0, .9fr); }
      .settings-card { display: grid; gap: 12px; align-content: start; }
      .settings-card:nth-child(3) { grid-column: 1 / -1; }
      .settings-card-head { display: flex; align-items: center; gap: 12px; min-width: 0; }
      .settings-card h3 { margin: 0; font-size: 13px; font-weight: 600; }
      .settings-card p { margin: 0; color: var(--muted); font-size: 12.5px; line-height: 1.55; }
      .settings-actions { display: flex; gap: 10px; }
      .settings-actions .primary-action { display: inline-flex; align-items: center; justify-content: center; }
      .settings-list { display: grid; grid-template-columns: 104px minmax(0, 1fr); gap: 9px 12px; margin: 0; color: var(--text); font-size: 12.5px; }
      .settings-list dt { color: var(--muted-2); }
      .settings-list dd { margin: 0; min-width: 0; overflow-wrap: anywhere; }
      .settings-list code { border-radius: 5px; background: var(--bg); color: var(--muted); padding: 2px 6px; font-size: 11.5px; }
      .dialog-note { margin: 0; }
      .status-line { margin: 0 auto 0 0; min-height: 16px; color: var(--muted-2); font-size: 11.5px; }
      .dialog-section { display: grid; gap: 12px; }
      .dialog-section h3 { margin: 0; font-size: 13px; font-weight: 600; }
      .toggle-row { min-height: 52px; display: flex; align-items: center; justify-content: space-between; gap: 16px; padding: 11px 13px; border: 1px solid var(--border-soft); border-radius: 10px; background: var(--bg); }
      .toggle-row span { color: var(--text); display: grid; gap: 4px; }
      .toggle-row strong { font-size: 13px; font-weight: 550; }
      .toggle-row small { color: var(--muted); font-size: 11.5px; line-height: 1.45; }
      /* Switches, not stock checkboxes — the dialogs are the one place the app asks
         the user to make a deliberate choice, so the controls should feel deliberate. */
      .toggle-row input[type=checkbox], .source-row input[type=checkbox] { appearance: none; -webkit-appearance: none; position: relative; width: 40px; height: 23px; flex: 0 0 auto; margin: 0; border: 1px solid var(--border); border-radius: 999px; background: var(--surface-3); cursor: pointer; transition: background .18s ease, border-color .18s ease, box-shadow .18s ease; }
      .toggle-row input[type=checkbox]::after, .source-row input[type=checkbox]::after { content: ''; position: absolute; top: 2px; left: 2px; width: 17px; height: 17px; border-radius: 999px; background: #d8d8de; box-shadow: 0 1px 3px rgba(0,0,0,.5); transition: transform .18s cubic-bezier(.2,.8,.3,1.2), background .18s ease; }
      .toggle-row input[type=checkbox]:checked, .source-row input[type=checkbox]:checked { background: var(--accent); border-color: transparent; }
      .toggle-row input[type=checkbox]:checked::after, .source-row input[type=checkbox]:checked::after { transform: translateX(17px); background: #fff; }
      .toggle-row input[type=checkbox]:focus-visible, .source-row input[type=checkbox]:focus-visible { box-shadow: var(--focus); }
      .toggle-row:has(input[type=checkbox]:checked) { border-color: rgba(229,72,77,.32); }
      .toggle-row { transition: border-color .2s ease, background .2s ease; }

      /* ---------- personalize feed dialog ---------- */
      @keyframes tune-in { from { opacity: 0; transform: translateY(10px) scale(.985); } to { opacity: 1; transform: none; } }
        var(--surface); transition: background .35s ease; }
      /* The thumb travels inset by half its own width at each end, so the fill has to
         use the same geometry or the colour stops short of (or runs past) the handle. */
      [data-tune-root].is-sliding .tune-range::-moz-range-thumb { transform: scale(1.22); }
      [data-tune-root].is-sliding .tune-readout { transform: scale(1.06); }
      /* Grid, not space-between: with flex the middle label centres between the two
         end labels rather than on the track's midpoint, which reads as crooked. */
      .topic-chip-list { display: flex; flex-wrap: wrap; gap: 6px; min-height: 0; }
      .topic-chip-list:empty { display: none; }
      .topic-chip-list button em { font-style: normal; opacity: .55; font-size: 13px; line-height: 1; }
      .topic-chip-list.muted button { border-color: rgba(255,255,255,.12); background: rgba(255,255,255,.05); color: var(--muted); text-decoration: line-through; text-decoration-color: rgba(255,255,255,.28); }
      @keyframes chip-in { from { opacity: 0; transform: translateY(3px) scale(.96); } to { opacity: 1; transform: none; } }
      @media (prefers-reduced-motion: reduce) {
      }
      @media (max-width: 720px) {
      }

      .dialog-actions { display: flex; justify-content: flex-end; align-items: center; gap: 8px; padding: 0 20px 20px; }
      .dialog-actions button { height: 34px; border: 0; border-radius: 999px; background: var(--surface-2); color: var(--text); padding: 0 16px; cursor: pointer; font-size: 13px; font-weight: 500; }
      .dialog-actions button:hover { background: var(--surface-3); }
      .dialog-actions .primary-action { background: #f1f1f3; color: #0a0a0b; font-weight: 600; }
      .dialog-actions .primary-action:hover { background: #fff; }
      .picker-list { display: grid; gap: 2px; max-height: 320px; overflow: auto; }
      .picker-row { display: grid; grid-template-columns: 32px minmax(0, 1fr) auto; align-items: center; gap: 12px; min-height: 46px; padding: 0 12px; border: 0; border-radius: 10px; background: transparent; color: var(--text); text-align: left; cursor: pointer; font-size: 13px; }
      .picker-row:hover { background: var(--surface-3); }
      .picker-row small { display: block; color: var(--muted); font-size: 11.5px; }
      .picker-row .icon { width: 16px; height: 16px; color: var(--muted); }
      .picker-empty { color: var(--muted); font-size: 12.5px; padding: 12px 10px; }
      .picker-create { display: flex; gap: 8px; align-items: center; padding-top: 12px; margin-top: 8px; border-top: 1px solid var(--border-soft); }
      .picker-create .primary-action { white-space: nowrap; flex: 0 0 auto; }

      /* ---------- generic pages ---------- */
      .app-page { padding-top: calc(var(--header) + 28px); }
      .page-head { display: flex; align-items: flex-end; justify-content: space-between; gap: 20px; margin-bottom: 4px; }
      .page-head h1 { margin: 0; font-size: 22px; font-weight: 650; letter-spacing: -.022em; }
      .page-head p { margin: 7px 0 0; color: var(--muted); font-size: 13px; line-height: 1.5; }
      .page-tabs { display: flex; align-items: center; gap: 24px; border-bottom: 1px solid var(--border-soft); margin: 22px 0 22px; overflow-x: auto; scrollbar-width: none; }
      .page-tab { position: relative; display: inline-flex; align-items: baseline; gap: 7px; padding: 0 0 12px; color: var(--muted); font-size: 15px; font-weight: 500; background: none; border: 0; cursor: pointer; white-space: nowrap; }
      .page-tab .count { font-size: 11.5px; color: var(--muted-2); font-variant-numeric: tabular-nums; }
      .page-tab:hover { color: var(--text); }
      .page-tab.active { color: var(--text); }
      .page-tab.active::after { content: ""; position: absolute; left: 0; right: 0; bottom: -1px; height: 2px; border-radius: 2px 2px 0 0; background: var(--accent); }
      .toolbar { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 24px; }
      .toolbar-divider { width: 1px; height: 20px; background: var(--border); }
      .toolbar-note { color: var(--muted); font-size: 12.5px; display: inline-flex; align-items: center; gap: 7px; }
      .toolbar-note .icon { width: 15px; height: 15px; }
      .toolbar-search { margin-left: auto; display: flex; align-items: center; gap: 8px; height: 32px; min-width: 224px; border: 1px solid var(--border); border-radius: 999px; background: var(--surface); padding: 0 14px; color: var(--muted-2); }
      .toolbar-search input { flex: 1; min-width: 0; border: 0; background: transparent; color: var(--text); outline: none; font-size: 12.5px; }
      .toolbar-search .icon { width: 15px; height: 15px; }
      .view-toggle { display: inline-flex; gap: 2px; }
      .view-toggle button { width: 32px; height: 30px; border: 1px solid transparent; border-radius: var(--r-control); background: transparent; color: var(--muted); display: grid; place-items: center; cursor: pointer; }
      .view-toggle button:hover { background: var(--surface-2); color: var(--text); }
      .view-toggle button.active { background: var(--surface-3); border-color: var(--border); color: var(--text); }
      .page-hero { display: flex; align-items: flex-end; justify-content: space-between; gap: 20px; margin-bottom: 8px; }
      .page-hero h1 { margin: 0; font-size: 22px; font-weight: 650; letter-spacing: -.022em; }
      .page-hero p { margin: 7px 0 0; color: var(--muted); font-size: 13px; line-height: 1.5; }
      .page-kicker { display: none; }
      .page-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
      .page-search { display: flex; align-items: center; width: min(640px, 100%); height: 36px; border: 1px solid var(--border); border-radius: var(--r-control); overflow: hidden; background: var(--surface); }
      .page-search input { flex: 1; min-width: 0; height: 100%; border: 0; outline: none; background: transparent; color: var(--text); padding: 0 14px; font-size: 13px; }
      .page-search button { width: 44px; height: 100%; border: 0; border-left: 1px solid var(--border-soft); background: transparent; color: var(--muted); display: grid; place-items: center; cursor: pointer; }
      .filter-strip { display: flex; align-items: center; gap: 8px; overflow-x: auto; padding-bottom: 20px; scrollbar-width: none; }
      .filter-strip::-webkit-scrollbar { display: none; }
      .result-list, .library-list { display: grid; gap: 4px; max-width: 1080px; }
      .media-row { display: grid; grid-template-columns: 228px minmax(0, 1fr) 22px; gap: 16px; align-items: start; padding: 8px; border-radius: 10px; }
      .media-row:hover { background: var(--surface); }
      .media-row .thumb { border-radius: var(--r-thumb); }
      .media-row-copy { min-width: 0; }
      .media-row h2 { margin: 0; font-size: 14.5px; font-weight: 550; line-height: 1.4; letter-spacing: -.01em; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
      .media-row p { margin: 6px 0 0; color: var(--muted); font-size: 12.5px; line-height: 1.4; }
      .row-index { display: grid; justify-items: center; color: var(--muted-2); font-size: 12px; font-variant-numeric: tabular-nums; padding-top: 10px; }

      /* ---------- timeline ---------- */
      .timeline { position: relative; padding-left: 24px; }
      .timeline::before { content: ""; position: absolute; left: 3px; top: 6px; bottom: 0; width: 1px; background: var(--border-soft); }
      .timeline-group { position: relative; margin-bottom: 44px; }
      .timeline-group::before { content: ""; position: absolute; left: -24px; top: 5px; width: 7px; height: 7px; border-radius: 999px; background: var(--accent); }
      .timeline-group.past::before { background: var(--surface-3); border: 1px solid var(--border); }
      .timeline-date { margin: 0 0 18px; color: var(--muted); font-size: 12px; font-weight: 500; letter-spacing: .01em; }
      .timeline-group .video-grid { grid-template-columns: repeat(auto-fill, minmax(272px, 1fr)); gap: 30px 20px; }

      /* ---------- channels / collections ---------- */
      .channel-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(156px, 1fr)); gap: 24px 12px; }
      /* Playlists are media tiles, not channel avatars. They used to borrow .channel-grid,
         whose 156px floor is sized for a circular avatar and a name, and a 16:9 cover with
         a title and a metadata line came out 185px wide with the meta wrapping onto two
         lines. This is the timeline grid's rhythm, so playlists sit on the same 3-up beat
         as every other media surface in the app. */
      .playlist-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(272px, 1fr)); gap: 30px 20px; }
      .channel-card { display: grid; justify-items: center; text-align: center; gap: 8px; padding: 14px 8px; border: 1px solid transparent; border-radius: var(--r-panel); min-width: 0; transition: background .12s ease, border-color .12s ease; }
      .channel-card:hover { background: var(--surface); border-color: var(--border-soft); }
      .channel-card-top { display: grid; justify-items: center; gap: 10px; min-width: 0; width: 100%; }
      .channel-card-top > div { min-width: 0; width: 100%; }
      .channel-card .avatar { width: 56px; height: 56px; font-size: 15px; }
      .channel-card h2 { margin: 0; font-size: 13px; font-weight: 550; line-height: 1.35; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .channel-card p { margin: 4px 0 0; color: var(--muted); font-size: 11.5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .subscribe-pill { height: 30px; border: 0; border-radius: 999px; background: var(--surface-2); color: var(--text); display: inline-flex; align-items: center; gap: 7px; padding: 0 13px; font-size: 12.5px; font-weight: 500; cursor: pointer; }
      .subscribe-pill:hover { background: var(--surface-3); }
      .subscribe-pill .icon { width: 14px; height: 14px; }
      .collection-block { padding: 24px 0; border-bottom: 1px solid var(--border-soft); }
      .collection-block:last-of-type { border-bottom: 0; }
      .collection-head { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 18px; }
      .collection-head h2 { margin: 0; font-size: 14px; font-weight: 600; display: inline-flex; align-items: center; gap: 8px; }
      .collection-head .icon { width: 14px; height: 14px; color: var(--muted-2); }
      .collection-actions { display: flex; align-items: center; gap: 8px; }
      .card-actions-inline { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; justify-content: center; }
      .collection-tools { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1.3fr); gap: 12px; margin-bottom: 26px; }
      .collection-form { border: 1px solid var(--border-soft); border-radius: var(--r-panel); background: var(--surface); padding: 14px; display: grid; grid-template-columns: minmax(120px, 1fr) minmax(120px, 1fr) auto; gap: 12px; align-items: end; }
      .collection-form label { display: grid; gap: 7px; color: var(--muted-2); font-size: 11.5px; }
      .collection-form input, .collection-form select { min-height: 32px; border: 1px solid var(--border); border-radius: var(--r-control); background: var(--bg); color: var(--text); padding: 0 10px; min-width: 0; font-size: 13px; }
      .stat-row { display: flex; align-items: center; gap: 18px; margin: 0 0 24px; color: var(--muted); font-size: 12.5px; }
      .stat { display: inline-flex; align-items: baseline; gap: 6px; }
      .stat strong { font-size: 13px; font-weight: 600; color: var(--text); font-variant-numeric: tabular-nums; }
      .stat span { color: var(--muted); }

      /* ---------- collections ---------- */
      .collection-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(232px, 1fr)); gap: 26px 20px; align-items: start; }
      .collection-card { min-width: 0; }
      .collection-card-cover { display: block; aspect-ratio: 16 / 10; border-radius: var(--r-thumb); overflow: hidden; border: 1px solid var(--border-soft); background: var(--surface); transition: border-color .12s ease, background .12s ease; }
      .collection-card:hover .collection-card-cover { background: var(--surface-2); border-color: var(--border); }
      .collection-cover { display: grid; width: 100%; height: 100%; padding: 14px; gap: 8px; grid-template-columns: repeat(2, auto); place-content: center; justify-items: center; }
      .collection-cover[data-tiles="3"] > :nth-child(3) { grid-column: span 2; }
      .collection-cover[data-tiles="1"] { grid-template-columns: 1fr; }
      .collection-cover[data-tiles="1"] .avatar { width: 56px; height: 56px; font-size: 16px; }
      .collection-cover[data-tiles="2"] { grid-template-columns: repeat(2, 1fr); }
      .collection-cover .avatar { width: 42px; height: 42px; font-size: 13px; }
      .collection-cover.empty { place-items: center; grid-template-columns: 1fr; color: var(--muted-2); }
      .collection-cover.empty .icon { width: 26px; height: 26px; }
      .collection-card-body { display: grid; grid-template-columns: minmax(0, 1fr) 22px; gap: 8px; align-items: start; padding-top: 11px; }
      .collection-card-body h3 { margin: 0; font-size: 14px; font-weight: 550; line-height: 1.4; letter-spacing: -.008em; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .collection-card-body p { margin: 5px 0 0; color: var(--muted); font-size: 12px; line-height: 1.4; }
      .collection-hero { display: flex; align-items: center; gap: 24px; padding: 4px 0 26px; border-bottom: 1px solid var(--border-soft); margin-bottom: 8px; }
      .collection-hero-cover { flex: 0 0 auto; width: 172px; aspect-ratio: 16 / 10; border: 1px solid var(--border-soft); border-radius: var(--r-panel); overflow: hidden; background: var(--surface); }
      .collection-hero-copy { min-width: 0; }
      .collection-hero-kicker { margin: 0 0 6px; color: var(--muted-2); font-size: 11px; font-weight: 600; letter-spacing: .08em; text-transform: uppercase; }
      .collection-hero h1 { margin: 0; font-size: clamp(20px, 2vw, 22px); font-weight: 650; letter-spacing: -.022em; }
      .collection-hero-meta { margin: 8px 0 0; color: var(--muted); font-size: 12.5px; }
      .collection-hero-description { margin: 10px 0 0; color: var(--muted); font-size: 13px; line-height: 1.55; max-width: 60ch; }
      .collection-hero-actions { margin-top: 16px; display: flex; gap: 8px; flex-wrap: wrap; }
      .collection-members .channel-card { position: relative; }
      .collection-remove { position: absolute; top: 6px; right: 6px; width: 32px; height: 32px; border: 0; border-radius: 999px; background: rgba(10,10,11,.8); color: var(--muted); display: grid; place-items: center; cursor: pointer; opacity: 0; transition: opacity .12s ease, color .12s ease; }
      .collection-members .channel-card:hover .collection-remove, .collection-remove:focus-visible { opacity: 1; }
      .collection-remove:hover { color: #f2686c; }
      .collection-remove .icon { width: 14px; height: 14px; }
      .picker-row.is-member .picker-check { color: var(--accent); }
      .picker-check { display: grid; place-items: center; width: 20px; }
      .picker-check .icon { width: 16px; height: 16px; }

      /* ---------- playlists ---------- */
      .playlist-card { position: relative; min-width: 0; }
      .playlist-cover { position: relative; display: block; aspect-ratio: 16 / 9; border-radius: var(--r-thumb); overflow: hidden; background: var(--surface-2); border: 1px solid var(--border-soft); }
      .playlist-cover::before, .playlist-cover::after { content: ""; position: absolute; left: 8px; right: 8px; border-radius: var(--r-thumb) var(--r-thumb) 0 0; background: var(--surface-3); border: 1px solid var(--border-soft); border-bottom: 0; }
      .playlist-cover::before { top: -7px; height: 8px; opacity: .5; left: 14px; right: 14px; }
      .playlist-cover::after { top: -3px; height: 6px; opacity: .8; }
      .playlist-cover img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; }
      .playlist-cover-empty { position: absolute; inset: 0; display: grid; place-items: center; color: var(--muted-2); }
      .playlist-cover-empty .icon { width: 22px; height: 22px; }
      /* Was a slab covering the right 42% of the cover, which at the old 185px card hid
         most of the artwork and only got heavier as the card grew. Same badge language as
         .duration instead: bottom-right, dark, small, out of the way of the picture. */
      .playlist-count { position: absolute; right: 6px; bottom: 6px; z-index: 2; display: inline-flex; align-items: center; gap: 5px; border-radius: 4px; background: rgba(0,0,0,.82); color: #fff; font-size: 11px; font-weight: 500; font-variant-numeric: tabular-nums; line-height: 1; padding: 3px 6px; }
      .playlist-count .icon { width: 13px; height: 13px; opacity: .9; }
      .playlist-body { display: grid; grid-template-columns: minmax(0, 1fr) 22px; gap: 8px; align-items: start; padding-top: 11px; }
      .playlist-body h3 { margin: 0; font-size: 13.5px; font-weight: 550; line-height: 1.4; letter-spacing: -.008em; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
      .playlist-body p { margin: 5px 0 0; color: var(--muted); font-size: 12px; line-height: 1.4; }
      .playlist-detail { display: grid; grid-template-columns: minmax(280px, 340px) minmax(0, 1fr); gap: 32px; align-items: start; }
      .playlist-panel { position: sticky; top: calc(var(--header) + 28px); border: 1px solid var(--border-soft); border-radius: var(--r-panel); background: var(--surface); padding: 18px; display: grid; gap: 14px; }
      .playlist-panel .playlist-cover { margin-top: 8px; }
      .playlist-panel h1 { margin: 0; font-size: 19px; font-weight: 650; letter-spacing: -.02em; line-height: 1.3; }
      .playlist-panel .playlist-meta { margin: 0; color: var(--muted); font-size: 12.5px; }
      .playlist-panel .playlist-description { margin: 0; color: var(--muted); font-size: 12.5px; line-height: 1.55; }
      .playlist-panel-actions { display: flex; gap: 8px; flex-wrap: wrap; }
      .playlist-items { display: grid; gap: 2px; }
      .playlist-items .media-row { grid-template-columns: 24px 168px minmax(0, 1fr) 22px 26px; gap: 14px; align-items: start; }
      .playlist-items .media-row h2 { font-size: 13.5px; }
      .playlist-remove { width: 32px; height: 32px; margin: 1px -3px -3px; border: 0; border-radius: 999px; background: transparent; color: var(--muted-2); display: grid; place-items: center; cursor: pointer; }
      .playlist-remove:hover { background: var(--surface-3); color: #f2686c; }

      /* ---------- channel page ---------- */
      /* YouTube caps channel content and centres it. Ours inherited the 2200px page width,
         which stretched the banner and left the identity block marooned against the
         gutter — the single biggest reason the page did not read like a channel. */
      /* An inner wrapper, not margin-inline on the main element: .home-main uses
         margin-left for the sidebar offset, and "auto" silently discarded it, sliding the
         whole page under the nav. */
      .channel-main .ch-wrap { max-width: 1320px; margin-inline: auto; }
      .channel-banner { aspect-ratio: 6.2 / 1; border-radius: 12px; overflow: hidden; background: var(--surface-2); margin-top: 4px; }
      .channel-banner img { width: 100%; height: 100%; object-fit: cover; display: block; }
      .channel-banner:not(.has-image) { display: none; }
      /* YouTube banner geometry. Invidious serves the desktop crop (2560x423, ~6.05:1).
         YouTube renders 2560x423 on desktop, 1855x423 on tablet and 1546x423 on mobile —
         all the same centred strip, progressively cropped at the sides. Matching those
         aspect ratios per breakpoint and cropping from the centre keeps the channel's
         "safe zone" (name, logo, tagline) on screen at every width, same as YouTube. */
      .channel-banner { aspect-ratio: 2560 / 423; width: 100%; border: 1px solid var(--border-soft); border-radius: var(--r-panel); background: linear-gradient(135deg, hsl(var(--avatar-hue, 240) 10% 15%), var(--bg) 74%); display: flex; align-items: center; justify-content: center; color: rgba(255,255,255,.14); font-size: clamp(18px, 2.6vw, 34px); font-weight: 700; letter-spacing: .24em; text-transform: uppercase; overflow: hidden; }
      .channel-hero { display: flex; align-items: center; gap: 22px; padding: 22px 0 10px; }
      .channel-hero > div { min-width: 0; }
      .channel-hero-avatar { width: 88px; height: 88px; font-size: 26px; }
      .channel-hero h1 { margin: 0; font-size: clamp(20px, 2vw, 22px); font-weight: 650; letter-spacing: -.022em; display: flex; align-items: center; gap: 8px; }
      .channel-hero-meta { margin: 7px 0 0; color: var(--muted); font-size: 12.5px; }
      .channel-hero-actions { margin-top: 14px; display: flex; gap: 8px; flex-wrap: wrap; }

      /* ---------- shorts ---------- */
      .section-heading h2 { display: inline-flex; align-items: center; gap: 8px; }
      .section-heading h2 .icon { width: 17px; height: 17px; color: var(--accent); }
      .shorts-row { display: grid; grid-auto-flow: column; grid-auto-columns: minmax(158px, 1fr); gap: 16px; overflow-x: auto; padding-bottom: 6px; scrollbar-width: none; }
      .shorts-row::-webkit-scrollbar { display: none; }
      .short-card { min-width: 0; }
      .short-thumb { position: relative; display: block; aspect-ratio: 9 / 16; border-radius: var(--r-thumb); overflow: hidden; background: #000; border: 1px solid var(--border-soft); }
      .short-thumb img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; }
      .short-badge { position: absolute; left: 8px; bottom: 8px; display: grid; place-items: center; width: 24px; height: 24px; border-radius: 6px; background: rgba(10,10,11,.72); color: #fff; backdrop-filter: blur(4px); }
      .short-badge .icon { width: 14px; height: 14px; }
      .short-copy { padding-top: 9px; }
      .short-copy h3 { margin: 0; font-size: 13px; font-weight: 550; line-height: 1.35; letter-spacing: -.006em; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
      .short-copy p { margin: 4px 0 0; color: var(--muted); font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      /* A Short is vertical; cap the player width so it is not pillarboxed inside a 16:9 frame. */
      .watch-player-shell.is-short { max-width: min(430px, 100%); margin-inline: auto; }
      .watch-player-shell.is-short .player-frame { aspect-ratio: 9 / 16; }

      /* ---------- shorts feed (immersive) ---------- */
      .shorts-main { padding: 0 0 0 0; margin-left: var(--sidebar); height: calc(100vh - var(--header)); margin-top: var(--header); overflow: hidden; }
      .shorts-viewport { position: relative; height: 100%; display: grid; grid-template-columns: minmax(0, 1fr) 0; transition: grid-template-columns .22s ease; }
      .shorts-viewport.comments-open { grid-template-columns: minmax(0, 1fr) clamp(300px, 26vw, 380px); }
      /* One Short per scroll stop, like a vertical feed. */
      .shorts-stage { height: 100%; overflow-y: auto; scroll-snap-type: y mandatory; scrollbar-width: none; overscroll-behavior: contain; }
      .shorts-stage::-webkit-scrollbar { display: none; }
      /* Player and action rail centre together as one unit. */
      .shorts-slide { position: relative; height: 100%; scroll-snap-align: center; scroll-snap-stop: always; display: grid; grid-template-columns: auto 64px; align-items: center; justify-content: center; gap: 10px; padding: 14px 10px; }
      .shorts-stage-inner { position: relative; height: 100%; aspect-ratio: 9 / 16; max-width: 100%; }
      .shorts-frame { position: relative; height: 100%; width: 100%; border-radius: 14px; overflow: hidden; background: #000; border: 1px solid var(--border-soft); }
      .shorts-frame iframe { position: absolute; inset: 0; width: 100%; height: 100%; border: 0; }
      .shorts-poster { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; }
      .shorts-play { position: absolute; inset: 0; margin: auto; width: 56px; height: 56px; border: 0; border-radius: 999px; background: rgba(10,10,11,.6); color: #fff; display: grid; place-items: center; cursor: pointer; backdrop-filter: blur(6px); transition: opacity .15s ease, transform .15s ease; }
      .shorts-play .icon { width: 24px; height: 24px; }
      .shorts-play:hover { transform: scale(1.05); }
      .shorts-slide.is-playing .shorts-play, .shorts-slide.is-playing .shorts-poster { opacity: 0; pointer-events: none; }
      .shorts-overlay { position: absolute; left: 0; right: 0; bottom: 0; padding: 46px 14px 16px; display: grid; gap: 7px; background: linear-gradient(to top, rgba(0,0,0,.82), rgba(0,0,0,0)); border-radius: 0 0 14px 14px; pointer-events: none; }
      .shorts-overlay > * { pointer-events: auto; }
      .shorts-channel { display: inline-flex; align-items: center; gap: 9px; color: #fff; font-size: 13px; font-weight: 600; width: max-content; max-width: 100%; }
      .shorts-channel .avatar { width: 30px; height: 30px; }
      .shorts-channel span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .shorts-title { margin: 0; font-size: 13.5px; font-weight: 500; line-height: 1.45; color: #fff; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
      .shorts-meta { margin: 0; color: rgba(255,255,255,.66); font-size: 12px; }
      .shorts-rail { align-self: center; justify-self: start; display: grid; gap: 4px; }
      .shorts-action { width: 56px; border: 0; border-radius: 12px; background: transparent; color: var(--text); display: grid; justify-items: center; gap: 4px; padding: 9px 0; cursor: pointer; font-size: 10px; text-align: center; transition: background .12s ease, color .12s ease; }
      .shorts-action:hover { background: var(--surface-2); }
      .shorts-action .icon { width: 22px; height: 22px; }
      .shorts-action.is-selected { color: var(--accent); }
      /* Same filled-thumb treatment as the watch page, so a like reads the same everywhere. */
      .shorts-action .icon { transition: fill .18s ease; }
      .shorts-action.is-selected .icon { fill: currentColor; }
      .shorts-action.is-bumped .icon { animation: thumb-pop .38s cubic-bezier(.2,1.55,.35,1); }
      .shorts-action.is-bumped[data-feedback="dislike"] .icon { animation-name: thumb-pop-down; }
      .shorts-nav-buttons { position: absolute; right: 14px; bottom: 20px; display: grid; gap: 8px; z-index: 5; }
      .shorts-nav { width: 34px; height: 34px; border: 1px solid var(--border); border-radius: 999px; background: rgba(18,18,20,.9); color: var(--text); display: grid; place-items: center; cursor: pointer; backdrop-filter: blur(6px); }
      .shorts-nav:hover { background: var(--surface-3); }
      .shorts-nav .icon { width: 17px; height: 17px; }
      .shorts-nav[disabled] { opacity: .35; cursor: default; }
      .shorts-comments { height: 100%; overflow: hidden; display: grid; grid-template-rows: auto minmax(0, 1fr); border-left: 1px solid var(--border-soft); background: var(--surface); }
      .shorts-comments[hidden] { display: none; }
      .shorts-comments-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; padding: 14px 14px 12px; border-bottom: 1px solid var(--border-soft); }
      .shorts-comments-head h2 { margin: 0; font-size: 14px; font-weight: 600; }
      .shorts-comments-body { overflow-y: auto; padding: 8px 14px 20px; }
      .shorts-comments-body .comment { grid-template-columns: 30px minmax(0, 1fr); gap: 10px; padding: 10px 0; }
      .shorts-comments-body .comment .avatar { width: 30px; height: 30px; }
      .shorts-comments-body .comment-body { font-size: 12.5px; }
      .shorts-enable { display: grid; gap: 10px; justify-items: start; padding: 16px 2px; color: var(--muted); font-size: 12.5px; line-height: 1.55; }
      @media (max-width: 900px) {
        .shorts-main { margin-left: 0; height: calc(100vh - var(--header) - 56px); }
        .shorts-viewport.comments-open { grid-template-columns: minmax(0, 1fr); }
        .shorts-slide { grid-template-columns: minmax(0, 1fr); padding: 0; }
        .shorts-stage-inner { width: 100%; height: 100%; aspect-ratio: auto; justify-self: stretch; }
        .shorts-frame { border-radius: 0; border: 0; }
        .shorts-overlay { padding-right: 76px; border-radius: 0; }
        .shorts-rail { position: absolute; right: 6px; bottom: 78px; z-index: 4; justify-self: end; }
        .shorts-action { background: rgba(10,10,11,.4); backdrop-filter: blur(4px); }
        .shorts-nav-buttons { display: none; }
        .shorts-comments { position: fixed; left: 0; right: 0; bottom: 0; top: 42%; z-index: 90; border-left: 0; border-top: 1px solid var(--border); border-radius: 16px 16px 0 0; }
      }

      /* ---------- notifications ---------- */
      .bell-wrap { position: relative; display: grid; place-items: center; }
      .bell-badge { position: absolute; top: 0; right: 0; min-width: 18px; height: 18px; padding: 0 4px; border-radius: 999px; background: var(--accent); color: #fff; font-size: 11px; font-weight: 700; line-height: 18px; text-align: center; pointer-events: none; }
      .bell-badge[hidden] { display: none; }
      .notification-row { display: grid; grid-template-columns: 34px minmax(0, 1fr) 62px; gap: 11px; align-items: start; padding: 9px 10px; border-radius: 8px; color: var(--text); }
      .notification-row:hover { background: var(--surface-3); }
      .notification-row.unread { background: rgba(229,72,77,.07); }
      .notification-row .avatar { width: 34px; height: 34px; }
      .notification-copy { min-width: 0; }
      .notification-copy strong { display: block; font-size: 12.5px; font-weight: 600; line-height: 1.35; }
      .notification-copy span { display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; color: var(--text); font-size: 12.5px; line-height: 1.4; margin-top: 2px; }
      .notification-copy small { display: block; color: var(--muted); font-size: 11px; margin-top: 4px; }
      .notification-thumb { position: relative; aspect-ratio: 16 / 9; border-radius: 6px; overflow: hidden; background: #000; }
      .notification-thumb img { width: 100%; height: 100%; object-fit: cover; }
      .notification-empty { color: var(--muted); font-size: 12.5px; padding: 16px 10px; text-align: center; }

      /* ---------- comments ---------- */
      .comments { margin-top: 24px; }
      .comments-head { display: flex; align-items: baseline; gap: 10px; margin-bottom: 18px; }
      .comments-head h2 { margin: 0; font-size: 16px; font-weight: 600; letter-spacing: -.012em; }
      .comments-count { color: var(--muted); font-size: 13px; font-variant-numeric: tabular-nums; }
      .comments-state { color: var(--muted); font-size: 13px; margin: 0; padding: 12px 0; }
      .comment { display: grid; grid-template-columns: 36px minmax(0, 1fr); gap: 14px; padding: 12px 0; }
      .comment .avatar { width: 36px; height: 36px; }
      .comment-head { display: flex; align-items: center; gap: 7px; flex-wrap: wrap; margin-bottom: 5px; }
      .comment-author { font-size: 12.5px; font-weight: 600; color: var(--text); }
      /* A creator's own comment is set apart the way YouTube does it: their handle gets a
         filled chip, and the whole comment a faint tint so it reads as authored, not just
         another reply. */
      .comment-author.owner { background: var(--text); color: var(--bg); border-radius: 999px; padding: 2px 9px; font-weight: 650; }
      .comment.is-creator { background: rgba(255,255,255,.028); border-radius: 12px; padding: 12px 14px; margin-left: -14px; }
      .comment-mention { color: #6ea8ff; font-weight: 550; }
      .comment-heart { position: relative; display: inline-grid; place-items: center; width: 22px; height: 22px; }
      .comment-heart img { width: 18px; height: 18px; border-radius: 999px; object-fit: cover; }
      .comment-heart i { position: absolute; right: -3px; bottom: -3px; font-size: 10px; font-style: normal;
        color: #f2686c; text-shadow: 0 0 3px rgba(0,0,0,.9); }
      .comment-time { color: var(--muted); font-size: 12px; }
      .comment-flag { color: var(--muted); font-size: 11px; display: inline-flex; align-items: center; gap: 4px; }
      .comment-flag .icon { width: 13px; height: 13px; }
      .comment-body { margin: 0; color: var(--text); font-size: 13.5px; line-height: 1.55; white-space: pre-wrap; overflow-wrap: anywhere; }
      /* A clickable timestamp, in a description or a comment. Styled as a link but built
         as a button, because it seeks the player rather than navigating anywhere. */
      .ts-link { border: 0; background: none; padding: 0; font: inherit; color: #6ea8ff; cursor: pointer; }
      .ts-link:hover, .ts-link:focus-visible { text-decoration: underline; }
      .watch-description a { color: #6ea8ff; overflow-wrap: anywhere; }
      .watch-description a:hover { text-decoration: underline; }
      /* Kept tight so the pair reads as one control. The like button carries a count and
         the dislike does not — YouTube publishes no per-comment dislike figure, and there
         is nothing to put there — so the gap between them is the only thing holding the
         two thumbs apart. */
      .comment-reactions { display: inline-flex; align-items: center; gap: 0; margin-left: -5px; }
      .comment-vote-count { font-size: 11.5px; }
      .comment-vote { display: inline-flex; align-items: center; gap: 4px; min-height: 26px; padding: 0 5px;
        border: 0; border-radius: 999px; background: transparent; color: var(--muted-2); font-size: 12px;
        font-variant-numeric: tabular-nums; cursor: pointer; transition: background .12s ease, color .12s ease; }
      .comment-vote:hover { background: var(--surface-3); color: var(--text); }
      .comment-vote .icon { width: 15px; height: 15px; transition: fill .16s ease, transform .16s ease; }
      .comment-vote.is-selected { color: var(--text); }
      .comment-vote.is-selected .icon { fill: currentColor; }
      .comment-vote .icon { transition: fill .18s ease; }
      .comment-vote.is-bumped .icon { animation: thumb-pop .38s cubic-bezier(.2,1.55,.35,1); }
      .comment-vote.is-bumped[data-comment-vote="dislike"] .icon { animation-name: thumb-pop-down; }
      @media (prefers-reduced-motion: reduce) { .comment-vote .icon { transition: none; } }
      .comment-meta { display: flex; align-items: center; gap: 14px; margin-top: 8px; color: var(--muted); font-size: 12px; }
      .comment-meta span { display: inline-flex; align-items: center; gap: 5px; }
      .comment-meta .icon { width: 14px; height: 14px; }

      /* ---------- watch ---------- */
      /* The player is a grid child rather than the first thing inside the article, which
         is what lets theater mode span it across both columns without moving any markup. */
      .watch-main { max-width: 1760px; padding-top: calc(var(--header) + 20px); padding-right: 32px;
        --watch-gutter: 32px; }
      .watch-layout { display: grid; grid-template-columns: minmax(0, 1fr) clamp(320px, 27vw, 392px);
        grid-template-areas: "stage panel" "content panel"; gap: 0 28px; align-items: start; min-width: 0; }
      .watch-layout > * { min-width: 0; }
      .watch-stage { grid-area: stage; }
      .watch-content { grid-area: content; }
      .watch-panel { grid-area: panel; }
      .watch-player-shell { position: relative; z-index: 1; background: #000; border: 1px solid var(--border-soft); border-radius: var(--r-panel); overflow: hidden; }
      .player-frame { width: 100%; max-width: 100%; aspect-ratio: 16 / 9; border: 0; background: #000; display: block; }
      .watch-title { margin: 16px 0 12px; font-size: 19px; font-weight: 650; line-height: 1.35; letter-spacing: -.018em; }
      /* Channel, subscribe and the actions share one row, the way YouTube arranges them.
         The actions are pushed right by an auto margin and drop to their own line only
         once the row genuinely runs out of width. */
      /* The margin here is load-bearing: the gap under this row used to come from
         .watch-actions, which lost it when the actions moved onto this line. Without it
         the description sits flush against the subscriber count and clips it. */
      .watch-meta-line { display: flex; align-items: center; gap: 12px 16px; flex-wrap: wrap;
        margin: 4px 0 12px; min-height: 40px; color: var(--muted); font-size: 13px; }
      .watch-channel { display: flex; align-items: center; gap: 11px; min-width: 0; }
      .watch-channel .avatar { width: 40px; height: 40px; }
      .watch-channel > div { min-width: 0; }
      .watch-creators { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; min-width: 0; }
      .creator-sep { color: var(--muted-2); font-size: 12px; }
      .watch-channel-name.is-plain { color: var(--muted); cursor: default; }
      .watch-channel-name { display: inline-block; max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text); font-size: 14px; font-weight: 600; letter-spacing: -.01em; }
      .watch-channel .metadata { font-size: 11.5px; margin-top: 2px; }
      .watch-inline-actions, .watch-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
      .watch-inline-actions { justify-content: flex-end; flex: 0 0 auto; }
      .watch-actions { margin: 0 0 0 auto; }

      /* One pill, used by every action on the page, so hover, press and focus behave
         identically whether the control is a button, a link or a <summary>. */
      .pill-action { min-height: 36px; border: 1px solid transparent; border-radius: 999px; background: var(--surface-2); color: var(--text);
        display: inline-flex; align-items: center; gap: 8px; padding: 0 15px; cursor: pointer; white-space: nowrap;
        font-size: 13px; font-weight: 550; letter-spacing: -.005em; list-style: none;
        transition: background .14s ease, border-color .14s ease, color .14s ease, transform .14s cubic-bezier(.2,.8,.3,1); }
      .pill-action:hover { background: var(--surface-3); border-color: var(--border-soft); }
      .pill-action:active { transform: scale(.96); }
      .pill-action:disabled { cursor: default; opacity: .55; }
      .pill-action .icon { width: 17px; height: 17px; }
      .pill-icon { width: 36px; padding: 0; justify-content: center; gap: 0; }

      /* ---------- subscribe + notification bell ---------- */
      .subscribe-group { display: inline-flex; align-items: center; }
      .subscribe-group.is-subscribed { background: var(--surface-2); border-radius: 999px; }
      .subscribe-group:not(.is-subscribed) .notify-menu { display: none; }
      .watch-subscribe { min-height: 36px; border: 0; border-radius: 999px; display: inline-flex; align-items: center; gap: 7px;
        padding: 0 18px; cursor: pointer; font-size: 13px; font-weight: 650; letter-spacing: -.006em;
        background: var(--text); color: var(--bg);
        transition: filter .14s ease, transform .14s cubic-bezier(.2,.8,.3,1), background .14s ease, color .14s ease; }
      .watch-subscribe .icon { width: 16px; height: 16px; }
      .watch-subscribe:hover { filter: brightness(.88); }
      .watch-subscribe:active { transform: scale(.96); }
      .watch-subscribe.subscribed { background: transparent; color: var(--text); font-weight: 550; border-radius: 999px 0 0 999px; padding-right: 14px; }
      .watch-subscribe.subscribed:hover { background: var(--surface-3); filter: none; }
      .notify-menu { display: inline-flex; }
      .notify-trigger { position: relative; min-height: 36px; display: inline-flex; align-items: center; gap: 1px;
        padding: 0 12px 0 10px; border-radius: 0 999px 999px 0; color: var(--text); cursor: pointer; list-style: none;
        transition: background .14s ease, color .14s ease; }
      .notify-trigger::-webkit-details-marker { display: none; }
      .notify-trigger::before { content: ""; position: absolute; left: 0; top: 9px; bottom: 9px; width: 1px; background: var(--border); }
      .notify-trigger:hover, .notify-menu[open] > .notify-trigger { background: var(--surface-3); }
      .notify-trigger.is-muted { color: var(--muted-2); }
      .notify-trigger .icon { width: 17px; height: 17px; }
      .notify-trigger .icon + .icon { width: 13px; height: 13px; color: var(--muted); transition: transform .18s ease; }
      .notify-menu[open] > .notify-trigger .icon + .icon { transform: rotate(180deg); }
      .notify-menu .menu-panel { right: 0; left: auto; top: 42px; width: min(288px, calc(100vw - 24px)); }
      .notify-menu.drop-up .menu-panel { top: auto; bottom: 42px; }

      /* ---------- reactions ---------- */
      .watch-reactions { display: inline-flex; align-items: center; background: var(--surface-2); border-radius: 999px; }
      .watch-reactions .pill-action { background: transparent; border-color: transparent; }
      .watch-reactions .pill-action:hover { background: var(--surface-3); }
      .watch-reactions .pill-action:first-child { border-radius: 999px 0 0 999px; padding-right: 13px; }
      .watch-reactions .pill-action:last-child { position: relative; border-radius: 0 999px 999px 0; padding-left: 13px; }
      .watch-reactions .pill-action:last-child::before { content: ""; position: absolute; left: 0; top: 9px; bottom: 9px; width: 1px; background: var(--border); }
      /* The outline is one closed subpath, so filling it is the whole "filled thumb"
         state — there is no second icon that could drift away from the outline one. */
      .reaction-action .icon { transition: fill .18s ease; }
      .reaction-action.is-selected { color: var(--text); }
      .reaction-action.is-selected .icon { fill: currentColor; }
      .reaction-action.is-bumped .icon { animation: thumb-pop .38s cubic-bezier(.2,1.55,.35,1); }
      .reaction-action.is-bumped[data-feedback="dislike"] .icon { animation-name: thumb-pop-down; }
      @keyframes thumb-pop {
        0% { transform: scale(1) rotate(0); }
        30% { transform: scale(1.3) rotate(-10deg); }
        62% { transform: scale(.93) rotate(4deg); }
        100% { transform: scale(1) rotate(0); }
      }
      @keyframes thumb-pop-down {
        0% { transform: scale(1) rotate(0); }
        30% { transform: scale(1.3) rotate(10deg); }
        62% { transform: scale(.93) rotate(-4deg); }
        100% { transform: scale(1) rotate(0); }
      }
      .reaction-count { font-size: 12.5px; font-variant-numeric: tabular-nums; color: var(--muted); }
      .reaction-action.is-selected .reaction-count { color: var(--text); }
      .reaction-count.muted { color: var(--muted-2); }
      /* An estimate is set slightly back from a real count so the two do not read alike. */
      .reaction-count.is-estimate { color: var(--muted); }
      .privacy-note { border-left: 2px solid var(--border); padding-left: 10px; margin-top: 4px; line-height: 1.55; }

      /* ---------- download picker ---------- */
      .download-dialog { width: min(460px, calc(100vw - 32px)); }
      .download-options { display: grid; gap: 6px; }
      .download-option { display: grid; grid-template-columns: 20px minmax(0, 1fr) auto; align-items: center; gap: 12px; min-height: 48px;
        border: 1px solid var(--border-soft); border-radius: 10px; background: var(--surface-2); color: var(--text);
        padding: 0 14px; cursor: pointer; text-align: left; font-size: 13px;
        transition: background .14s ease, border-color .14s ease, transform .14s cubic-bezier(.2,.8,.3,1); }
      .download-option:hover { background: var(--surface-3); border-color: var(--border); }
      .download-option:active { transform: scale(.985); }
      .download-option .icon { width: 17px; height: 17px; color: var(--muted); }
      .download-option strong { display: block; font-size: 13.5px; font-weight: 600; }
      .download-option small { display: block; color: var(--muted-2); font-size: 11.5px; margin-top: 2px; }
      .download-size { color: var(--muted-2); font-size: 12px; font-variant-numeric: tabular-nums; }
      /* ---------- video details ---------- */
      .details-dialog { width: min(560px, calc(100vw - 32px)); }
      .details-dialog .video-details > summary { display: none; }
      .details-dialog .video-details { margin: 0; border-top: 0; padding-top: 0; }
      .video-details { margin-top: 14px; border-top: 1px solid var(--border-soft); padding-top: 12px; }
      .video-details > summary { list-style: none; cursor: pointer; display: inline-flex; align-items: center; gap: 7px;
        color: var(--muted); font-size: 12.5px; font-weight: 600; }
      .video-details > summary::-webkit-details-marker { display: none; }
      .video-details > summary:hover { color: var(--text); }
      .video-details > summary .icon { width: 15px; height: 15px; }
      .video-details[open] > summary { margin-bottom: 12px; color: var(--text); }
      .detail-list { display: grid; grid-template-columns: repeat(auto-fill, minmax(210px, 1fr)); gap: 10px 20px; margin: 0; }
      .detail-row { display: grid; gap: 2px; }
      .detail-row dt { color: var(--muted-2); font-size: 11.5px; }
      .detail-row dd { margin: 0; font-size: 13px; font-variant-numeric: tabular-nums; }
      .detail-tag { display: inline-block; margin-left: 5px; padding: 1px 6px; border-radius: 999px;
        background: var(--surface-3); color: var(--muted); font-size: 10.5px; font-weight: 600; }
      .detail-tags { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 14px; }
      .detail-chip { padding: 4px 10px; border-radius: 999px; background: var(--surface-2); color: var(--muted);
        font-size: 11.5px; transition: background .12s ease, color .12s ease; }
      .detail-chip:hover { background: var(--surface-3); color: var(--text); }

      /* ---------- channel page ---------- */
      .ch-identity { display: grid; grid-template-columns: 160px minmax(0, 1fr); gap: 28px; align-items: center;
        margin: 24px 0 0; }
      .ch-avatar { width: 160px; height: 160px; font-size: 44px; }
      .ch-identity-copy { min-width: 0; display: grid; gap: 8px; }
      .ch-identity h1 { margin: 0; font-size: 36px; font-weight: 700; letter-spacing: -.03em; display: flex; align-items: center; gap: 8px; line-height: 1.15; }
      .ch-identity h1 .verified { width: 17px; height: 17px; color: var(--muted); }
      .ch-meta { margin: 0; color: var(--muted); font-size: 14px; }
      .ch-handle { color: var(--text); font-weight: 550; }
      .ch-dot { color: var(--muted-2); }
      /* One clamped line with "...more" trailing it. Deliberately not expandable in place:
         this sits in a grid cell next to the avatar, so growing it shifts the avatar. */
      .ch-about { display: flex; align-items: baseline; gap: 4px; color: var(--muted); font-size: 14px;
        line-height: 1.45; max-width: 620px; }
      .ch-about p { margin: 0; }
      .ch-about-short { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .ch-about-more { flex: none; border: 0; background: none; padding: 0; color: var(--text); font-size: 14px;
        font-weight: 600; cursor: pointer; }
      .ch-about-more:hover { text-decoration: underline; }
      .ch-about a { color: #6ea8ff; }
      .ch-links { display: flex; align-items: center; gap: 7px; margin: 0; font-size: 14px; }
      .ch-links .icon { width: 15px; height: 15px; color: var(--muted); flex: none; }
      .ch-links a { color: #6ea8ff; }

      /* Scoped to [open]: a bare `display: flex` on a dialog beats the browser's own
         "closed dialogs are hidden" rule, and the panel renders inline on page load. */
      .ch-about-dialog[open] { width: min(560px, calc(100vw - 32px)); max-height: min(86vh, 780px);
        display: flex; flex-direction: column; }
      .ch-about-dialog .fc-head { align-items: center; }
      .ch-about-dialog .fc-head h2 { margin: 0; font-size: 20px; font-weight: 650; letter-spacing: -.02em; }
      .ch-about-dialog .fc-body { overflow-y: auto; padding: 4px 22px 22px; display: grid; gap: 0; }
      .ch-about-section { padding: 18px 0 0; }
      .ch-about-section h3 { margin: 0 0 10px; font-size: 15px; font-weight: 650; letter-spacing: -.01em; }
      .ch-about-text { color: var(--text); font-size: 14px; line-height: 1.5; white-space: pre-line;
        overflow-wrap: anywhere; }
      .ch-about-text a { color: #6ea8ff; }
      .ch-link-list, .ch-info-list { list-style: none; margin: 0; padding: 0; display: grid; gap: 14px; }
      .ch-link-list li, .ch-info-list li { display: flex; align-items: center; gap: 14px; min-width: 0; }
      .ch-link-list .icon, .ch-info-list .icon { width: 20px; height: 20px; color: var(--muted); flex: none; }
      .ch-link-list div { min-width: 0; display: grid; gap: 1px; }
      .ch-link-name { font-size: 14px; font-weight: 550; }
      .ch-link-list a, .ch-info-list a { color: #6ea8ff; font-size: 14px; overflow-wrap: anywhere; }
      .ch-info-list span { font-size: 14px; }
      .ch-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-top: 8px; }
      .ch-menu { display: inline-flex; }
      .ch-menu .menu-panel { left: 0; right: auto; top: 40px; }

      .ch-tabs { display: flex; align-items: stretch; justify-content: space-between; gap: 28px; margin: 22px 0 0;
        padding: 0 2px; border-bottom: 1px solid var(--border-soft); }
      .ch-tab-row { display: flex; align-items: center; gap: 28px; min-width: 0; overflow-x: auto;
        scrollbar-width: none; }
      .ch-tab-row::-webkit-scrollbar { display: none; }
      .ch-tab { position: relative; padding: 13px 0; color: var(--muted); font-size: 15px; font-weight: 550; letter-spacing: -.005em; }
      .ch-tab:hover { color: var(--text); }
      .ch-tab.active { color: var(--text); }
      .ch-tab.active::after { content: ""; position: absolute; left: 0; right: 0; bottom: -1px; height: 2px; background: var(--text); }
      /* YouTube's channel search is a bare magnifier that grows into an underlined field.
         The old pill drew a small outlined box floating in the middle of the tab row. */
      .ch-search { flex: none; display: flex; align-items: center; gap: 10px; align-self: center; }
      .ch-search-toggle { display: inline-flex; align-items: center; justify-content: center; width: 34px; height: 34px;
        padding: 0; border: 0; border-radius: 50%; background: none; color: var(--muted); cursor: pointer;
        transition: background .12s ease, color .12s ease; }
      .ch-search-toggle:hover { background: var(--surface-2); color: var(--text); }
      .ch-search-toggle .icon { width: 20px; height: 20px; }
      .ch-search.is-open .ch-search-toggle { color: var(--text); }
      .ch-search input { width: 0; padding: 5px 0; border: 0; border-bottom: 1px solid transparent;
        background: none; color: var(--text); font-size: 14px; outline: none; opacity: 0;
        transition: width .18s ease, opacity .12s ease; }
      .ch-search.is-open input { width: min(240px, 34vw); opacity: 1; border-bottom-color: var(--border); }
      .ch-search.is-open input:focus { border-bottom-color: var(--text); }
      .ch-search input::-webkit-search-cancel-button { filter: invert(.6); }

      .channel-main .video-grid { margin-top: 18px; }
      .ch-filters { display: flex; gap: 8px; margin: 20px 0 6px; }
      .ch-chip { display: inline-flex; align-items: center; min-height: 34px; padding: 0 14px; border-radius: 999px;
        background: var(--surface-2); color: var(--text); font-size: 13.5px; font-weight: 500; }
      .ch-chip:hover { background: var(--surface-3); }
      .ch-chip.active { background: var(--text); color: var(--bg); }

      @media (max-width: 760px) {
        .ch-identity { grid-template-columns: 1fr; justify-items: center; text-align: center; gap: 14px; }
        .ch-identity-copy { justify-items: center; }
        .ch-avatar { width: 96px; height: 96px; }
        .ch-identity h1 { font-size: 24px; }
        .ch-tabs { gap: 12px; }
        .ch-tab-row { gap: 18px; }
        .ch-search.is-open input { width: 42vw; }
        .ch-about { max-width: none; }
      }

      /* ---------- feed controls ----------
         Deliberately plain. The panel this replaced animated a gradient from cyan to
         magenta as the slider moved, which had nothing to do with the rest of the
         interface and made a settings panel look like a screensaver. */
      .fc-dialog[open] { width: min(720px, calc(100vw - 40px)); max-height: min(88vh, 880px); display: flex; flex-direction: column; }
      .fc-dialog form { display: flex; flex-direction: column; min-height: 0; }
      .fc-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px;
        padding: 20px 22px 16px; border-bottom: 1px solid var(--border-soft); }
      .fc-kicker { margin: 0 0 4px; color: var(--muted-2); font-size: 11px; font-weight: 600; letter-spacing: .07em; text-transform: uppercase; }
      .fc-head h2 { margin: 0; font-size: 18px; font-weight: 650; letter-spacing: -.02em; }
      .fc-body { overflow-y: auto; padding: 4px 22px 8px; display: grid; gap: 4px; }
      .fc-section { padding: 16px 0; border-bottom: 1px solid var(--border-soft); }
      .fc-section:last-child { border-bottom: 0; }
      .fc-section h3 { margin: 0 0 8px; font-size: 13.5px; font-weight: 600; display: flex; align-items: baseline; justify-content: space-between; gap: 12px; }
      .fc-value { color: var(--accent); font-size: 13px; font-variant-numeric: tabular-nums; }
      .fc-caption { margin: 8px 0 0; color: var(--muted); font-size: 12px; line-height: 1.55; }

      /* Measured composition. One bar, two real numbers, counted not derived. */
      .fc-measure { display: grid; gap: 9px; }
      .fc-bar { height: 8px; border-radius: 999px; background: var(--surface-3); overflow: hidden; }
      .fc-bar-fill { display: block; height: 100%; background: var(--accent); border-radius: 999px; }
      .fc-legend { display: flex; justify-content: space-between; gap: 14px; color: var(--muted); font-size: 12px; }
      .fc-legend b { color: var(--text); font-variant-numeric: tabular-nums; margin-left: 4px; }
      .fc-dot { width: 8px; height: 8px; border-radius: 999px; display: inline-block; margin-right: 7px; }
      .fc-dot.subs { background: var(--accent); }
      .fc-dot.disc { background: var(--surface-3); border: 1px solid var(--border); }

      .fc-slider { width: 100%; height: 4px; margin: 12px 0 6px; appearance: none; border-radius: 999px;
        background: linear-gradient(to right, var(--accent) var(--fill, 35%), var(--surface-3) var(--fill, 35%)); cursor: pointer; }
      .fc-slider::-webkit-slider-thumb { appearance: none; width: 16px; height: 16px; border-radius: 999px;
        background: var(--text); border: 3px solid var(--accent); cursor: grab; }
      .fc-slider::-moz-range-thumb { width: 16px; height: 16px; border-radius: 999px; background: var(--text);
        border: 3px solid var(--accent); cursor: grab; }
      .fc-scale { display: flex; justify-content: space-between; color: var(--muted-2); font-size: 11px; }

      .fc-field { display: grid; gap: 5px; margin-bottom: 10px; }
      .fc-field span { color: var(--muted); font-size: 12px; }
      .fc-field input { height: 36px; padding: 0 12px; border: 1px solid var(--border); border-radius: 9px;
        background: var(--surface-2); color: var(--text); font-size: 13px; }
      .fc-field input:focus-visible { border-color: var(--accent); }

      .fc-channels { display: flex; flex-wrap: wrap; gap: 7px; margin-top: 10px; }
      .fc-channel { display: inline-flex; align-items: center; gap: 7px; min-height: 32px; padding: 0 11px;
        border: 1px solid var(--border); border-radius: 999px; background: var(--surface-2); color: var(--text);
        font-size: 12.5px; cursor: pointer; }
      .fc-channel:hover { background: var(--surface-3); }
      .fc-channel small { color: var(--muted-2); font-variant-numeric: tabular-nums; }
      .fc-channel-mark { display: none; }
      .fc-channel-mark .icon { width: 13px; height: 13px; }
      .fc-channel.is-muted { border-color: var(--accent); background: var(--accent-soft); color: var(--muted); text-decoration: line-through; }
      .fc-channel.is-muted .fc-channel-mark { display: grid; place-items: center; color: var(--accent); }

      .fc-signals { display: flex; flex-wrap: wrap; gap: 7px; margin-top: 10px; }
      .fc-signal { display: inline-flex; align-items: baseline; gap: 7px; padding: 5px 11px; border-radius: 999px;
        background: var(--surface-2); border: 1px solid var(--border-soft); font-size: 12px; }
      .fc-signal b { font-weight: 550; }
      .fc-signal i { color: var(--muted-2); font-style: normal; font-variant-numeric: tabular-nums; }
      .fc-signal.muted b { color: var(--muted-2); font-weight: 500; }

      .fc-foot { display: flex; align-items: center; justify-content: flex-end; gap: 10px;
        padding: 14px 22px; border-top: 1px solid var(--border-soft); }
      .fc-foot .status-line { margin-right: auto; }

      /* ---------- community posts ---------- */
      .post-list { display: grid; gap: 14px; max-width: 760px; }
      .post-list.is-shelf { grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); max-width: none; }
      .post-card { border: 1px solid var(--border-soft); border-radius: var(--r-panel); background: var(--surface); padding: 16px 18px; }
      .post-card.is-compact { padding: 14px; }
      .post-head { display: grid; grid-template-columns: 36px minmax(0,1fr) auto; gap: 11px; align-items: center; margin-bottom: 10px; }
      .post-head .avatar { width: 36px; height: 36px; }
      .post-head strong { display: block; font-size: 13.5px; font-weight: 600; }
      .post-head small { color: var(--muted-2); font-size: 11.5px; }
      .post-flag { margin-left: 6px; padding: 1px 6px; border-radius: 999px; background: var(--surface-3); font-size: 10px; }
      .post-permalink { color: var(--muted); display: grid; place-items: center; width: 28px; height: 28px; border-radius: 8px; }
      .post-permalink:hover { background: var(--surface-3); color: var(--text); }
      .post-body { font-size: 13.5px; line-height: 1.6; overflow-wrap: anywhere; }
      .post-card.is-compact .post-body { display: -webkit-box; -webkit-line-clamp: 4; -webkit-box-orient: vertical; overflow: hidden; }
      .post-meta { margin: 10px 0 0; color: var(--muted-2); font-size: 12px; }
      .post-images { display: grid; gap: 6px; margin-top: 12px; }
      .post-images.is-multi { grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); }
      .post-images img { width: 100%; border-radius: 10px; display: block; }
      .post-video { display: grid; grid-template-columns: 168px minmax(0,1fr); gap: 12px; margin-top: 12px;
        padding: 8px; border: 1px solid var(--border-soft); border-radius: 10px; background: var(--surface-2); }
      .post-video-thumb { position: relative; aspect-ratio: 16/9; border-radius: 8px; overflow: hidden; background: #000; }
      .post-video-thumb img { width: 100%; height: 100%; object-fit: cover; }
      .post-video-copy { display: grid; align-content: center; gap: 4px; min-width: 0; }
      .post-video-copy strong { font-size: 13px; line-height: 1.35; }
      .post-video-copy small { color: var(--muted); font-size: 11.5px; }
      .post-playlist { display: flex; align-items: center; gap: 10px; margin-top: 12px; padding: 11px 13px;
        border: 1px solid var(--border-soft); border-radius: 10px; background: var(--surface-2); }
      .post-unsupported { display: flex; gap: 8px; align-items: flex-start; margin: 12px 0 0; padding: 10px 12px;
        border: 1px dashed var(--border); border-radius: 10px; color: var(--muted); font-size: 12px; line-height: 1.5; }
      .post-unsupported .icon { width: 15px; height: 15px; flex: 0 0 auto; margin-top: 1px; }
      .post-more { margin-top: 18px; }

      /* Poll. The bar is a local tally, never YouTube's split — which it does not publish. */
      .post-poll { display: grid; gap: 6px; margin-top: 12px; }
      .poll-choice { position: relative; display: flex; align-items: center; gap: 10px; min-height: 40px;
        padding: 0 12px; border: 1px solid var(--border); border-radius: 10px; background: var(--surface-2);
        color: var(--text); font-size: 13px; text-align: left; cursor: pointer; overflow: hidden; }
      .poll-choice:hover { background: var(--surface-3); }
      .poll-bar { position: absolute; inset: 0 auto 0 0; width: 0; background: var(--accent-soft); transition: width .28s ease; }
      .poll-choice.is-picked { border-color: var(--accent); }
      .poll-choice.is-picked .poll-bar { width: 100%; }
      .poll-text { position: relative; flex: 1 1 auto; min-width: 0; }
      .poll-mark { position: relative; opacity: 0; color: var(--accent); display: grid; place-items: center; }
      .poll-choice.is-picked .poll-mark { opacity: 1; }
      .poll-mark .icon { width: 15px; height: 15px; }
      .poll-note { display: flex; gap: 8px; align-items: flex-start; margin: 6px 0 0; padding: 10px 12px;
        border: 1px solid var(--border-soft); border-radius: 10px; background: var(--surface-2);
        color: var(--muted); font-size: 11.5px; line-height: 1.55; }
      .poll-note .icon { width: 14px; height: 14px; flex: 0 0 auto; margin-top: 2px; }
      .poll-note strong { color: var(--text); }

      /* ---------- sponsorblock ---------- */
      .settings-divider { height: 1px; margin: 6px 0 2px; background: var(--border-soft); }
      .sb-categories { display: grid; gap: 2px; margin-top: 4px; }
      .sb-categories[hidden] { display: none; }
      .sb-row { display: flex; align-items: center; justify-content: space-between; gap: 16px; padding: 9px 0; }
      .sb-row + .sb-row { border-top: 1px solid var(--border-soft); }
      .sb-row span { display: grid; gap: 2px; min-width: 0; }
      .sb-row strong { font-size: 13px; font-weight: 550; }
      .sb-row small { color: var(--muted); font-size: 11.5px; line-height: 1.45; }
      .sb-row select { flex: 0 0 auto; height: 30px; padding: 0 8px; border: 1px solid var(--border);
        border-radius: 8px; background: var(--surface-2); color: var(--text); font-size: 12.5px; cursor: pointer; }
      /* The timeline markers and the manual skip button are NOT styled here. They are
         created inside the player embed, which is a separate document that never loads
         this stylesheet — their rules live in EMBED_CSS in the player shell, and putting
         copies here only makes it look like they are covered. The skip notice reuses the
         ordinary feedback toast. */

      /* ---------- status ---------- */
      .status-hero { display: flex; align-items: flex-start; justify-content: space-between; gap: 20px; flex-wrap: wrap; }
      .status-overall { display: inline-flex; align-items: center; gap: 9px; padding: 9px 15px; border-radius: 999px;
        border: 1px solid var(--border); background: var(--surface-2); font-size: 13px; font-weight: 600; }
      .status-overall.ok { border-color: rgba(63,185,120,.4); background: rgba(63,185,120,.1); color: #7ee2a8; }
      .status-overall.bad { border-color: rgba(229,72,77,.45); background: var(--accent-soft); color: #f2686c; }
      .status-overall .icon { width: 17px; height: 17px; }
      .status-block { margin-top: 30px; }
      .status-head { margin-bottom: 14px; }
      .status-head h2 { margin: 0; font-size: 16px; font-weight: 600; }
      .status-head p { margin: 4px 0 0; color: var(--muted); font-size: 12.5px; }
      .checks-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 10px; }
      .check { display: flex; align-items: center; justify-content: space-between; gap: 12px; min-height: 74px;
        padding: 13px 15px; border: 1px solid var(--border-soft); border-radius: var(--r-panel); background: var(--surface); }
      .check.ok { border-left: 3px solid #3fb978; }
      .check.bad { border-left: 3px solid var(--accent); background: var(--accent-soft); }
      .check h3 { margin: 0 0 3px; font-size: 13.5px; font-weight: 600; }
      .check p { margin: 0; color: var(--muted-2); font-size: 11.5px; line-height: 1.45; overflow-wrap: anywhere; }
      .check-badge { font-size: 11px; font-weight: 700; letter-spacing: .04em; color: var(--muted); }
      .check.ok .check-badge { color: #7ee2a8; }
      .check.bad .check-badge { color: #f2686c; }
      .stat-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 10px; }
      .stat-tile { display: grid; gap: 3px; padding: 13px 15px; border: 1px solid var(--border-soft);
        border-radius: var(--r-panel); background: var(--surface); }
      .stat-tile.ok { border-left: 3px solid #3fb978; }
      .stat-tile.warn { border-left: 3px solid #d9a441; }
      .stat-label { color: var(--muted); font-size: 11.5px; font-weight: 500; }
      .stat-value { font-size: 19px; font-weight: 650; letter-spacing: -.02em; overflow-wrap: anywhere; }
      .stat-tile small { color: var(--muted-2); font-size: 11px; overflow-wrap: anywhere; }
      /* Tables scroll inside their own box so a long message can never widen the page. */
      .status-scroll { overflow-x: auto; border: 1px solid var(--border-soft); border-radius: var(--r-panel); background: var(--surface); }
      .status-scroll table { width: 100%; border-collapse: collapse; font-size: 12.5px; min-width: 560px; }
      .status-scroll th, .status-scroll td { padding: 9px 14px; text-align: left; border-bottom: 1px solid var(--border-soft); vertical-align: top; }
      .status-scroll th { color: var(--muted); font-weight: 600; font-size: 11.5px; letter-spacing: .02em; }
      .status-scroll tr:last-child td { border-bottom: 0; }
      .status-scroll .bad-text { color: #f2686c; }

      /* ---------- takeout import ---------- */
      .import-note { display: flex; gap: 9px; align-items: flex-start; margin-top: 12px !important;
        padding: 11px 13px; border: 1px solid var(--border-soft); border-radius: 10px;
        background: var(--surface-2); line-height: 1.55; }
      .import-note .icon { width: 15px; height: 15px; flex: 0 0 auto; margin-top: 2px; color: var(--muted); }
      .import-note strong { color: var(--text); }
      .link-action { border: 0; background: none; padding: 0; color: #6ea8ff; font: inherit; cursor: pointer; text-decoration: underline; }
      .link-action:hover { color: #9cc4ff; }
      .import-status-actions { display: flex; gap: 8px; flex-wrap: wrap; }
      .import-layout { display: grid; grid-template-columns: minmax(0, 1.15fr) minmax(320px, .85fr); gap: 32px; align-items: start; }
      .import-steps { margin: 0; padding: 0 0 0 20px; display: grid; gap: 18px; counter-reset: none; }
      .import-steps li { padding-left: 6px; }
      .import-steps h3 { margin: 0 0 4px; font-size: 14px; font-weight: 600; }
      .import-steps p { margin: 0; color: var(--muted); font-size: 13px; line-height: 1.6; }
      .import-steps a { color: #6ea8ff; }
      .import-steps code { background: var(--surface-2); border-radius: 5px; padding: 1px 5px; font-size: 12px; }
      .import-panel { position: sticky; top: calc(var(--header) + 20px); display: grid; gap: 14px;
        border: 1px solid var(--border-soft); border-radius: var(--r-panel); background: var(--surface); padding: 18px; }
      .import-drop { display: grid; place-items: center; gap: 6px; min-height: 190px; padding: 20px; cursor: pointer;
        border: 1.5px dashed var(--border); border-radius: 12px; background: var(--surface-2); text-align: center; }
      .import-drop:hover, .import-drop.is-over { border-color: var(--accent); background: var(--accent-soft); }
      .import-drop .icon { width: 26px; height: 26px; color: var(--muted); }
      .import-drop strong { font-size: 13.5px; }
      .import-drop small { color: var(--muted-2); font-size: 12px; }
      .import-progress[hidden], .import-result[hidden] { display: none; }
      .import-result { border-radius: 10px; padding: 14px; background: var(--surface-2); font-size: 13px; line-height: 1.6; }
      .import-result.is-error { background: rgba(229,72,77,.1); }
      .import-result ul { margin: 8px 0 0; padding-left: 18px; color: var(--muted); }
      .import-status { display: flex; align-items: center; justify-content: space-between; gap: 16px; flex-wrap: wrap;
        border: 1px solid var(--border-soft); border-radius: var(--r-panel); background: var(--surface); padding: 14px 18px; margin-bottom: 24px; }
      .import-status p { margin: 3px 0 0; color: var(--muted); font-size: 12.5px; }
      .pill-action.danger { color: #f2686c; }
      @media (max-width: 1000px) {
        .import-layout { grid-template-columns: 1fr; }
        .import-panel { position: static; }
      }

      .download-destination { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-bottom: 14px; }
      .download-destination[hidden] { display: none; }
      .destination-option { display: grid; gap: 3px; text-align: left; padding: 10px 12px; border: 1px solid var(--border);
        border-radius: 10px; background: var(--surface-2); color: var(--text); cursor: pointer; }
      .destination-option:hover { background: var(--surface-3); }
      .destination-option.active { border-color: var(--accent); background: var(--accent-soft); }
      .destination-option strong { font-size: 13px; font-weight: 600; }
      .destination-option small { color: var(--muted); font-size: 11.5px; line-height: 1.4; }
      @media (max-width: 560px) { .download-destination { grid-template-columns: 1fr; } }

      /* A downloaded card is an ordinary card with a chip over the thumbnail. */
      .video-card { position: relative; }
      .download-chip { position: absolute; top: 8px; left: 8px; z-index: 3; display: inline-flex; align-items: center; gap: 6px;
        padding: 3px 4px 3px 8px; border-radius: 999px; background: rgba(10,10,11,.82); color: #ededef;
        font-size: 11px; font-weight: 600; backdrop-filter: blur(6px); }
      .download-chip .icon { width: 13px; height: 13px; }
      .download-remove { display: grid; place-items: center; width: 22px; height: 22px; border: 0; border-radius: 999px;
        background: transparent; color: var(--muted); cursor: pointer; }
      .download-remove:hover { background: rgba(255,255,255,.12); color: #f2686c; }
      .download-remove .icon { width: 13px; height: 13px; }

      .download-progress { display: grid; gap: 10px; }
      .download-progress[hidden] { display: none; }
      .download-progress-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
      .download-progress-head strong { font-size: 13.5px; font-weight: 600; }
      .download-bar { height: 6px; border-radius: 999px; background: var(--surface-3); overflow: hidden; }
      .download-bar span { display: block; height: 100%; width: 0; border-radius: 999px; background: var(--accent);
        transition: width .3s ease; }
      .download-bar.is-indeterminate span { width: 40%; animation: download-sweep 1.1s ease-in-out infinite; }
      @keyframes download-sweep { 0% { transform: translateX(-100%); } 100% { transform: translateX(250%); } }
      @media (prefers-reduced-motion: reduce) {
        .download-bar span { transition: none; }
        .download-bar.is-indeterminate span { animation: none; width: 100%; opacity: .5; }
      }

      /* ---------- theater mode ---------- */
      /* Same shape YouTube uses: the player widens to span both columns at its
         natural 16:9, and everything below it keeps the two-column split so up-next
         sits beside the description, not under it. The width is fitted to the
         viewport height, so on short windows the player shrinks (centered on the
         page background) instead of running off-screen; on tall windows it takes
         the full width. Either way the shell itself stays 16:9, so the video never
         letterboxes inside it. Unlike the full-bleed attempt, the player stays
         inside the page gutter with its radius and border, so it reads as a bigger
         player rather than a takeover. */
      body.theater .watch-layout { grid-template-areas: "stage stage" "content panel"; }
      /* The page normally holds a 20px gap below the header; in theater the player has to
         meet it, so that breathing room is dropped for the duration. */
      body.theater .watch-main { padding-top: var(--header); }
      body.theater .watch-stage { background: transparent;
        margin: 0 0 14px; }
      /* The stage already spaces the player from what follows, so the title's own top
         margin would stack on top of it — and because the up-next rail has no such margin,
         that extra 16px also knocked the two columns out of line. Dropping it fixes the
         gap and the alignment together: both columns now start on the same row. */
      body.theater .watch-title { margin-top: 0; }
      /* Natural 16:9 spanning both columns, but fitted to the viewport with just the
         title block peeking below: width is the smaller of full width and what leaves
         ~120px under the player, so the title, channel row and actions sit at the
         bottom edge — the way YouTube's theater does — with the description and the
         rest of up-next one scroll away. The shell itself stays 16:9 either
         way, so the video never letterboxes inside it; on short windows the player just
         shrinks and centers on the page background. The player is square-cornered like
         YouTube's theater — any round here visibly eats into the video edges. */
      body.theater .watch-player-shell { width: min(100%, calc((100vh - var(--header) - 120px) * 16 / 9)); max-width: 100%; margin-inline: auto;
        height: auto; aspect-ratio: 16 / 9; border-radius: 0; }
      /* The iframe follows the shell exactly. Height comes from the shell (which has
         the ratio), not from a second ratio computation on the frame itself — two
         competing aspect-ratios is what made resizing stutter. */
      body.theater .player-frame { width: 100%; height: 100%; }
      /* A 9:16 short letterboxed into a wide box would be almost entirely black, so cap
         its width and keep it in its own ratio. */
      body.theater .watch-player-shell.is-short { height: auto; aspect-ratio: 9 / 16; margin-inline: auto;
        max-width: min(430px, calc((100vh - var(--header) - 120px) * 9 / 16)); }
      /* Sidebar open/close animates the player width elsewhere on the site; in theater
         the shell tracks the window live, so transitioning it there only rubber-bands. */
      body.nav-ready:not(.theater) .watch-player-shell { transition: max-width .2s cubic-bezier(.2,.8,.3,1), border-radius .2s ease; }
      .theater-toggle .icon { transition: transform .18s ease; }
      .theater-toggle.is-selected { background: var(--surface-3); border-color: var(--border-soft); }
      .theater-toggle.is-selected .icon { transform: scaleX(1.15); }
      @media (max-width: 900px) { .watch-main { --watch-gutter: 16px; } }
      @media (prefers-reduced-motion: reduce) {
        .pill-action, .watch-subscribe, .download-option { transition: none; }
        .pill-action:active, .watch-subscribe:active, .download-option:active { transform: none; }
        .reaction-action.is-bumped .icon { animation: none; }
      }

      /* ---------- comment replies ---------- */
      .reply-toggle { display: inline-flex; align-items: center; gap: 6px; min-height: 28px; margin-top: 6px; padding: 0 10px 0 8px;
        border: 0; border-radius: 999px; background: transparent; color: #6ea8ff; font-size: 12.5px; font-weight: 550; cursor: pointer; }
      .reply-toggle:hover { background: rgba(110,168,255,.12); }
      .reply-toggle .icon { width: 15px; height: 15px; transition: transform .16s cubic-bezier(.2,.8,.3,1); }
      .reply-toggle.is-open .icon { transform: rotate(180deg); }
      .comment-replies { display: grid; gap: 14px; margin: 10px 0 0 4px; padding-left: 14px; border-left: 1px solid var(--border-soft); }
      .comment-replies[hidden] { display: none; }
      .comment-replies .comment { gap: 10px; }
      .comment-replies .comment.reply-depth-1 { margin-left: 20px; }
      .comment-replies .comment.reply-depth-2 { margin-left: 40px; }
      .comment-replies .comment.reply-depth-3 { margin-left: 60px; }
      .comment-replies .avatar { width: 26px; height: 26px; font-size: 9.5px; }
      @media (prefers-reduced-motion: reduce) { .reply-toggle .icon { transition: none; } }
      .watch-description { border: 1px solid var(--border-soft); border-radius: var(--r-panel); background: var(--surface); padding: 14px 16px; color: var(--text); line-height: 1.6; font-size: 13px; }
      .watch-description p { margin: 0; }
      .watch-description p + p { margin-top: 8px; }
      .watch-description-meta { color: var(--muted-2); font-size: 12px; font-weight: 500; margin-bottom: 10px !important; }
      .watch-description-body { overflow: hidden; }
      /* line-clamp rather than a max-height: clamping by ems cut the last line through
   its descenders, so g and p lost their tails. */
      .watch-description.is-collapsed .watch-description-body { display: -webkit-box;
        -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden;
        /* A hair of extra line-height so descenders on the clamped line are not shaved. */
        line-height: 1.62; }
      .description-toggle { margin-top: 10px; min-height: 24px; border: 0; border-radius: 6px; background: transparent; color: var(--muted); font-size: 12.5px; font-weight: 600; cursor: pointer; padding: 0; }
      .description-toggle:hover { color: var(--text); }
      .description-empty { color: var(--muted-2); }
      .transcript-row { width: 100%; border: 0; border-radius: 8px; background: transparent; color: var(--text); display: grid; grid-template-columns: 52px minmax(0, 1fr); gap: 10px; padding: 8px; text-align: left; cursor: pointer; }
      .transcript-row:hover { background: var(--surface-2); }
      .transcript-row span { color: var(--muted-2); font-size: 11.5px; font-weight: 600; font-variant-numeric: tabular-nums; }
      .transcript-row p { margin: 0; color: var(--muted); font-size: 12.5px; line-height: 1.45; }
      .watch-panel { position: sticky; top: calc(var(--header) + 20px); max-height: calc(100vh - var(--header) - 36px); overflow: auto; padding: 0 2px 12px 0; min-width: 0; max-width: 100%; scrollbar-width: thin; }
      .watch-panel-head { display: none; }
      .watch-panel .section-heading { margin-bottom: 12px; }
      .watch-panel .media-row { grid-template-columns: 160px minmax(0, 1fr) 22px; gap: 10px; padding: 6px; border-radius: 8px; }
      .watch-panel .media-row .thumb { border-radius: 8px; }
      .watch-panel .media-row h2 { font-size: 13px; font-weight: 550; line-height: 1.4; -webkit-line-clamp: 2; }
      .watch-panel .media-row p { margin-top: 4px; font-size: 11.5px; line-height: 1.35; }
      .watch-panel .menu-trigger { width: 22px; height: 22px; }
      .tabbar { display: flex; align-items: center; gap: 6px; margin-bottom: 14px; overflow-x: auto; scrollbar-width: none; }
      .tabbar::-webkit-scrollbar { display: none; }
      .tab { flex: 0 0 auto; min-height: 34px; border: 0; border-radius: 999px; background: var(--surface-2); color: var(--text); cursor: pointer; padding: 0 14px; max-width: 160px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13px; }
      .tab:hover { background: var(--surface-3); }
      .tab.active { background: #f1f1f3; color: #0a0a0b; font-weight: 500; }
      .tab-panel { display: none; }
      .tab-panel.active { display: grid; gap: 6px; }
      .details-list { display: grid; grid-template-columns: 92px minmax(0, 1fr); gap: 10px; color: var(--text); font-size: 12.5px; border: 1px solid var(--border-soft); border-radius: 10px; background: var(--surface); padding: 14px; }
      .details-list dt { color: var(--muted-2); }
      .details-list dd { margin: 0; overflow-wrap: anywhere; }
      .chat-placeholder { color: var(--muted); line-height: 1.6; padding: 8px; font-size: 12.5px; }
      .empty-copy { padding: 18px; border: 1px solid var(--border-soft); border-radius: var(--r-panel); background: var(--surface); color: var(--muted); font-size: 13px; line-height: 1.55; }
      .empty-copy a, .empty-copy .link-action { color: #6ea8ff; }
      .empty-copy a:hover, .empty-copy .link-action:hover { color: #9cc4ff; }
      .channel-grid > .empty-copy, .video-grid > .empty-copy, .timeline > .empty-copy, .playlist-grid > .empty-copy { grid-column: 1 / -1; }
      .mobile-bottom-nav { display: none; }
      .sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }

      /* ---------- responsive ---------- */
      @media (max-width: 1300px) {
        .shelf-track { grid-auto-columns: calc((100% - 2 * 16px) / 3); }
      }
      @media (max-width: 1100px) {
        :root { --sidebar: 68px; }
        .desktop-sidebar { padding: 8px 6px; }
        .nav-item { flex-direction: column; gap: 5px; min-height: 60px; border-radius: 10px; padding: 10px 0; font-size: 9.5px; text-align: center; }
        .nav-item.active::before { display: none; }
        .nav-item span { max-width: 58px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .nav-group-title, .sidebar-meta, .nav-dot { display: none; }
        .nav-channel { flex-direction: row; justify-content: center; min-height: 40px; padding: 0; }
        .nav-channel span { display: none; }
        .media-row { grid-template-columns: 188px minmax(0, 1fr) 22px; }
        .channel-banner { aspect-ratio: 1855 / 423; }
        .playlist-detail { grid-template-columns: 1fr; }
        .playlist-panel { position: static; }
      }
      @media (max-width: 1000px) {
        /* One column, so the named areas have to collapse with it — in and out of theater. */
        .watch-layout, body.theater .watch-layout { grid-template-columns: 1fr; grid-template-areas: "stage" "content" "panel"; }
        body.theater .watch-stage { margin-bottom: 16px; }
        .watch-panel { position: static; max-height: none; overflow: visible; margin-top: 24px; }
        .shelf-track { grid-auto-columns: calc((100% - 16px) / 2); }
      }
      @media (max-width: 900px) {
        :root { --header: 52px; --sidebar: 0px; }
        .desktop-sidebar { display: none; }
        .app-header { grid-template-columns: auto 1fr auto; padding: 0 12px; gap: 8px; }
        .header-center .search-form { display: none; }
        .header-actions .optional { display: none; }
        .compact-only { display: grid; }
        .nav-toggle { display: none; }
        .home-main { margin-left: 0; padding: calc(var(--header) + 14px) 16px calc(80px + env(safe-area-inset-bottom)); }
        .app-page { padding-top: calc(var(--header) + 18px); }
        .topic-wrap { top: var(--header); margin: -14px -16px 14px; padding: 10px 16px; }
        .creator-strip { display: flex; }
        .section-block { margin-top: 28px; }
        .shelf { padding: 14px; }
        .shelf-track { grid-auto-columns: 86%; }
        .shelf-nav { display: none; }
        .video-grid, .timeline-group .video-grid, .playlist-grid { grid-template-columns: 1fr; gap: 24px; }
        .collection-grid { grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 20px 12px; }
        .collection-hero { flex-direction: column; align-items: flex-start; gap: 16px; }
        .collection-hero-cover { width: 140px; }
        .collection-remove { opacity: 1; }
        .page-tabs { gap: 20px; }
        .page-head, .page-hero { flex-direction: column; align-items: stretch; }
        .page-head h1, .page-hero h1 { font-size: 19px; }
        .toolbar-search { margin-left: 0; width: 100%; }
        .timeline { padding-left: 18px; }
        .timeline-group::before { left: -18px; }
        .page-search { width: 100%; }
        .stat-row { flex-wrap: wrap; gap: 12px; }
        .media-row { grid-template-columns: minmax(132px, 42%) minmax(0, 1fr) 22px; gap: 10px; padding: 6px 0; }
        .playlist-items .media-row { grid-template-columns: 18px minmax(112px, 34%) minmax(0, 1fr) 20px 24px; gap: 8px; }
        .channel-grid { grid-template-columns: repeat(auto-fill, minmax(112px, 1fr)); }
        .playlist-grid { grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 22px 14px; }
        .channel-card .avatar { width: 48px; height: 48px; font-size: 13px; }
        .channel-banner { border-radius: 10px; }
        .channel-hero { flex-direction: column; align-items: flex-start; gap: 14px; }
        .channel-hero-avatar { width: 68px; height: 68px; font-size: 20px; }
        .collection-tools, .collection-form { grid-template-columns: 1fr; }
        .watch-main { padding: calc(var(--header) + 12px) 16px calc(80px + env(safe-area-inset-bottom)); }
        .watch-player-shell { margin: 0 -16px; border-radius: 0; border-left: 0; border-right: 0; }
        .watch-title { font-size: 16px; }
        .watch-meta-line { align-items: flex-start; flex-direction: column; }
        .watch-inline-actions { justify-content: flex-start; width: 100%; }
        /* Stacked, the auto margin would shove the actions to the right-hand edge. */
        .watch-actions { margin: 0; width: 100%; }
        .watch-actions { overflow-x: auto; flex-wrap: nowrap; padding-bottom: 4px; }
        .watch-panel .media-row { grid-template-columns: minmax(132px, 42%) minmax(0, 1fr) 22px; }
        .mobile-bottom-nav { position: fixed; left: 0; right: 0; bottom: 0; z-index: 80; height: calc(56px + env(safe-area-inset-bottom)); padding-bottom: env(safe-area-inset-bottom); background: rgba(10,10,11,.94); backdrop-filter: blur(12px); border-top: 1px solid var(--border-soft); display: grid; grid-template-columns: repeat(5, 1fr); }
        .mobile-nav-item { display: grid; place-items: center; align-content: center; gap: 4px; color: var(--muted); font-size: 9.5px; min-width: 0; padding: 6px 2px; }
        .mobile-nav-item .icon { width: 19px; height: 19px; }
        .mobile-nav-item span { max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .mobile-nav-item[aria-current="page"] { color: var(--text); }
        .feedback-toast { bottom: calc(72px + env(safe-area-inset-bottom)); left: 16px; right: 16px; transform: none; justify-content: space-between; }
        .feedback-toast.show { transform: none; }
      }
      @media (max-width: 700px) {
        /* Phones get YouTube's mobile crop (1546x423) — the centred safe zone only. */
        .channel-banner { aspect-ratio: 1546 / 423; }
      }
      @media (max-width: 430px) {
        .menu-panel, .watch-menu .menu-panel { position: fixed; left: 8px; right: 8px; bottom: calc(66px + env(safe-area-inset-bottom)); top: auto; width: auto; max-height: min(72vh, 620px); border-radius: var(--r-panel); }
        .feed-dialog { width: calc(100vw - 16px); }
        .dialog-grid, .settings-grid { grid-template-columns: 1fr; }
        .dialog-card:nth-child(3), .settings-card:nth-child(3) { grid-column: auto; }
        .settings-list { grid-template-columns: 1fr; }
      }
      @media (prefers-reduced-motion: reduce) {
        *, *::before, *::after { scroll-behavior: auto !important; transition-duration: 0.01ms !important; animation-duration: 0.01ms !important; }
      }
    '''


def _home_script() -> str:
    # The two thumbs come from the server's own icon table rather than being spelled out
    # again here. A hand-copied duplicate had already drifted from it, which is exactly
    # how the like and dislike buttons end up not matching.
    return f'''
      const LIKE_ICON = '{_icon("like")}';
      const DISLIKE_ICON = '{_icon("dislike")}';
    ''' + '''
      const PLAYLIST_ICON = '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 6h12"/><path d="M4 12h12"/><path d="M4 18h7"/><path d="M18 15v6"/><path d="M15 18h6"/></svg>';
      const PLUS_ICON = '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14"/><path d="M5 12h14"/></svg>';
      const CHECK_ICON = '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="m5 12 4 4L19 6"/></svg>';
      const COLLECTIONS_ICON = '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="4" width="7" height="7" rx="2"/><rect x="14" y="4" width="7" height="7" rx="2"/><rect x="3" y="13" width="7" height="7" rx="2"/><rect x="14" y="13" width="7" height="7" rx="2"/></svg>';
      const CHEVRON_ICON = '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg>';
      const ICON_DOWNLOAD = '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M7 10l5 5 5-5"/><path d="M12 15V3"/></svg>';
      /* Short names match the server-rendered trigger label; the longer ones are what the
         toast says, where there is room for a full sentence. */
      const NOTIFY_LABELS = { all: 'All', personalized: 'Personalized', none: 'None' };
      const NOTIFY_TOASTS = { all: 'All notifications', personalized: 'Personalized notifications', none: 'Notifications off' };
      const NOTIFY_ICONS = {
        all: '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M10.268 21a2 2 0 0 0 3.464 0"/><path d="M22 8c0-2.3-.8-4.3-2-6"/><path d="M3.262 15.326A1 1 0 0 0 4 17h16a1 1 0 0 0 .74-1.673C19.41 13.956 18 12.499 18 8A6 6 0 0 0 6 8c0 4.499-1.411 5.956-2.738 7.326"/><path d="M4 2C2.8 3.7 2 5.7 2 8"/></svg>',
        personalized: '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M10.268 21a2 2 0 0 0 3.464 0"/><path d="M3.262 15.326A1 1 0 0 0 4 17h16a1 1 0 0 0 .74-1.673C19.41 13.956 18 12.499 18 8A6 6 0 0 0 6 8c0 4.499-1.411 5.956-2.738 7.326"/></svg>',
        none: '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M10.268 21a2 2 0 0 0 3.464 0"/><path d="M17 17H4a1 1 0 0 1-.74-1.673C4.59 13.956 6 12.499 6 8a6 6 0 0 1 .258-1.742"/><path d="m2 2 20 20"/><path d="M8.668 3.01A6 6 0 0 1 18 8c0 2.687.77 4.653 1.707 6.05"/></svg>'
      };
      const toast = document.querySelector('[data-feedback-toast]');
      const dialog = document.getElementById('feed-dialog');
      const settingsDialog = document.getElementById('settings-dialog');
      const feedContainer = document.querySelector('[data-feed-container]');
      const loading = document.querySelector('[data-feed-loading]');
      const retryBox = document.querySelector('[data-feed-retry]');
      const sentinel = document.querySelector('[data-feed-sentinel]');
      let feedLoading = false;
      let feedHasMore = true;

      function safeCss(value) {
        if (window.CSS && typeof CSS.escape === 'function') return CSS.escape(value);
        return String(value).replace(/[^a-zA-Z0-9_-]/g, '\\$&');
      }

      /* Hover previews: dwelling on a card thumb swaps in a muted inline player backed
         by /api/preview, the same way the platform previews on hover. Shorts keep their
         own feed, touch devices have no hover, and reduced-motion users never opted in,
         so all three sit out. One preview at a time; anything unplayable fails silent
         and the card keeps its thumbnail. */
      (function initHoverPreviews() {
        if (!window.matchMedia || !matchMedia('(hover: hover) and (pointer: fine)').matches) return;
        if (matchMedia('(prefers-reduced-motion: reduce)').matches) return;
        const INTENT_MS = 450;
        let armed = null;
        let active = null;

        function killPreview() {
          if (armed) { window.clearTimeout(armed.timer); armed = null; }
          if (!active) return;
          const { card, video } = active;
          active = null;
          try { video.pause(); } catch (_) { /* already gone */ }
          video.removeAttribute('src');
          video.load();
          video.remove();
          const bar = card.querySelector('a.thumb > .preview-progress');
          if (bar) bar.remove();
          card.classList.remove('is-previewing');
        }

        function startPreview(card) {
          killPreview();
          const thumb = card.querySelector('a.thumb');
          const videoId = card.getAttribute('data-video-id') || '';
          if (!thumb || !/^[A-Za-z0-9_-]{11}$/.test(videoId)) return;
          if ((thumb.getAttribute('href') || '').indexOf('/shorts') === 0) return;
          const video = document.createElement('video');
          video.className = 'card-preview';
          video.muted = true;
          video.playsInline = true;
          video.preload = 'auto';
          video.setAttribute('aria-hidden', 'true');
          video.setAttribute('tabindex', '-1');
          const bar = document.createElement('span');
          bar.className = 'preview-progress';
          bar.setAttribute('aria-hidden', 'true');
          const fill = document.createElement('i');
          bar.appendChild(fill);
          video.addEventListener('canplay', () => { video.play().catch(() => {}); });
          video.addEventListener('error', () => { if (active && active.video === video) killPreview(); }, true);
          video.addEventListener('timeupdate', () => {
            if (video.duration) fill.style.width = `${(100 * video.currentTime / video.duration).toFixed(1)}%`;
          });
          thumb.appendChild(video);
          thumb.appendChild(bar);
          card.classList.add('is-previewing');
          active = { card, video };
          video.src = `/api/preview/${videoId}`;
        }

        document.addEventListener('mouseover', (event) => {
          const card = event.target && event.target.closest ? event.target.closest('article[data-video-card]') : null;
          if (active && active.card === card) return;
          killPreview();
          if (!card) return;
          armed = { card, timer: window.setTimeout(() => { armed = null; startPreview(card); }, INTENT_MS) };
        });
        document.addEventListener('mouseout', (event) => {
          const card = event.target && event.target.closest ? event.target.closest('article[data-video-card]') : null;
          if (!card) return;
          const to = event.relatedTarget && event.relatedTarget.closest ? event.relatedTarget.closest('article[data-video-card]') : null;
          if (card !== to) killPreview();
        });
        window.addEventListener('pagehide', killPreview);
      })();

      function showToast(message, action) {
        if (!toast) return;
        toast.replaceChildren();
        const text = document.createElement('span');
        text.textContent = message;
        toast.appendChild(text);
        if (action && action.label && typeof action.onClick === 'function') {
          const button = document.createElement('button');
          button.type = 'button';
          button.textContent = action.label;
          button.addEventListener('click', action.onClick, { once: true });
          toast.appendChild(button);
        }
        toast.classList.add('show');
        window.clearTimeout(showToast.timer);
        showToast.timer = window.setTimeout(() => toast.classList.remove('show'), action ? 5200 : 1800);
      }

      function updateReactionButtons(videoId, reaction) {
        document.querySelectorAll(`[data-feedback][data-video-id="${safeCss(videoId)}"]`).forEach((button) => {
          const eventType = button.dataset.feedback;
          if (eventType !== 'like' && eventType !== 'dislike') return;
          const selected = reaction === eventType;
          const wasSelected = button.classList.contains('is-selected');
          button.classList.toggle('is-selected', selected);
          button.setAttribute('aria-pressed', selected ? 'true' : 'false');
          /* Bump only on the transition into selected, and via a class the server never
             renders, so a page that loads already-liked does not animate on arrival. */
          if (selected && !wasSelected) {
            button.classList.remove('is-bumped');
            void button.offsetWidth;
            button.classList.add('is-bumped');
          } else if (!selected) {
            button.classList.remove('is-bumped');
          }
        });
      }

      function resetMenuPanel(menu) {
        const panel = menu.querySelector('.menu-panel');
        if (!panel) return;
        ['position', 'left', 'right', 'top', 'bottom'].forEach((prop) => panel.style.removeProperty(prop));
      }

      function closeMenus(exceptMenu) {
        document.querySelectorAll('.yt-menu[open]').forEach((menu) => {
          if (menu !== exceptMenu) {
            menu.removeAttribute('open');
            menu.classList.remove('drop-up');
            resetMenuPanel(menu);
          }
        });
      }

      function positionMenu(menu) {
        if (!menu || window.matchMedia('(max-width: 430px)').matches) return;
        const summary = menu.querySelector('summary');
        const panel = menu.querySelector('.menu-panel');
        if (!summary || !panel || !menu.open) return;
        menu.classList.remove('drop-up');
        panel.style.removeProperty('--menu-max-height');
        const summaryBox = summary.getBoundingClientRect();
        const below = Math.max(180, window.innerHeight - summaryBox.bottom - 12);
        const above = Math.max(180, summaryBox.top - 12);
        let dropUp = false;
        if (below < 380 && above > below) {
          dropUp = true;
          menu.classList.add('drop-up');
          panel.style.setProperty('--menu-max-height', `${Math.min(above, window.innerHeight - 24)}px`);
        } else {
          panel.style.setProperty('--menu-max-height', `${Math.min(below, window.innerHeight - 24)}px`);
        }
        /* Menus inside a horizontally scrolling shelf would be clipped, so pin them to the viewport. */
        if (menu.closest('.shelf-track')) {
          const width = panel.offsetWidth || 300;
          panel.style.position = 'fixed';
          panel.style.right = 'auto';
          panel.style.left = `${Math.max(8, Math.min(summaryBox.right - width, window.innerWidth - width - 8))}px`;
          if (dropUp) {
            panel.style.top = 'auto';
            panel.style.bottom = `${window.innerHeight - summaryBox.top + 6}px`;
          } else {
            panel.style.bottom = 'auto';
            panel.style.top = `${summaryBox.bottom + 6}px`;
          }
        } else {
          resetMenuPanel(menu);
        }
      }

      function currentFilter() {
        return feedContainer?.dataset.feedFilter || document.querySelector('[data-filter].active')?.dataset.filter || 'all';
      }

      function setActiveFilter(filter) {
        document.querySelectorAll('[data-filter]').forEach((chip) => {
          chip.classList.toggle('active', (chip.dataset.filter || 'all') === filter);
        });
        if (feedContainer) feedContainer.dataset.feedFilter = filter;
      }

      function dedupeIncomingHtml(html) {
        if (!feedContainer || !html) return html;
        const known = new Set(Array.from(feedContainer.querySelectorAll('[data-video-id]')).map((card) => card.dataset.videoId).filter(Boolean));
        const wrapper = document.createElement('div');
        wrapper.innerHTML = html;
        wrapper.querySelectorAll('[data-video-card]').forEach((card) => {
          const videoId = card.dataset.videoId;
          if (videoId && known.has(videoId)) {
            card.remove();
          } else if (videoId) {
            known.add(videoId);
          }
        });
        wrapper.querySelectorAll('.feed-section').forEach((section) => {
          if (!section.querySelector('[data-video-card]')) section.remove();
        });
        return wrapper.innerHTML;
      }

      async function loadFeedPage(options = {}) {
        if (!feedContainer || feedLoading || (!feedHasMore && !options.reset)) return;
        const filter = options.filter || currentFilter();
        const page = options.reset ? 0 : Number(feedContainer.dataset.nextPage || '1');
        feedLoading = true;
        if (loading) loading.hidden = false;
        if (retryBox) retryBox.hidden = true;
        try {
          const response = await fetch(`/api/home/sections?page=${page}&filter=${encodeURIComponent(filter)}`);
          if (!response.ok) throw new Error('section load failed');
          const data = await response.json();
          let html = options.reset ? data.html : dedupeIncomingHtml(data.html || '');
          if (options.reset) {
            feedContainer.innerHTML = html || '<p class="empty-copy">No videos match this filter yet. <button class="link-action" type="button" data-clear-filter>Clear filter</button></p>';
          } else if (html) {
            feedContainer.insertAdjacentHTML('beforeend', html);
          }
          feedContainer.dataset.nextPage = String(data.next_page || page + 1);
          feedContainer.dataset.feedFilter = filter;
          feedHasMore = Boolean(data.has_more && (html || options.reset));
        } catch (error) {
          if (retryBox) retryBox.hidden = false;
          if (options.reset && feedContainer && !feedContainer.children.length) {
            feedContainer.innerHTML = '<p class="empty-copy">Could not load recommendations. <button class="link-action" type="button" data-retry-feed>Retry</button></p>';
          }
        } finally {
          feedLoading = false;
          if (loading) loading.hidden = true;
        }
      }

      function parseTopics(value) {
        return String(value || '').split(',').map((item) => item.trim().toLowerCase()).filter(Boolean);
      }

      function fadeRemove(node) {
        if (!node) return;
        node.classList.add('is-removing');
        window.setTimeout(() => node.remove(), 220);
      }

      /* Unsubscribing only deletes the local subscription row: the channel's videos are
         still cached locally, so every subscription-backed surface on this page has
         to drop them itself. */
      function dropUnsubscribedChannel(channelId, title) {
        if (!channelId) return;
        const match = `[data-channel-id="${safeCss(channelId)}"]`;
        let removed = 0;

        document.querySelectorAll(`article.channel-card${match}`).forEach((card) => {
          fadeRemove(card);
          removed += 1;
        });

        /* Anything shown *because* of the subscription goes, wherever it sits —
           including the subscription uploads blended into the recommendation run. */
        document.querySelectorAll(`[data-video-card]${match}[data-source="subscription"]`).forEach((card) => {
          fadeRemove(card);
          removed += 1;
        });

        const scopes = document.querySelectorAll('[data-section-kind="subscription"], [data-section-kind="catch-up"]');
        scopes.forEach((scope) => {
          scope.querySelectorAll(`[data-video-card]${match}`).forEach((card) => {
            fadeRemove(card);
            removed += 1;
          });
        });

        window.setTimeout(() => {
          scopes.forEach((scope) => {
            if (!scope.querySelector('[data-video-card]')) fadeRemove(scope.closest('.section-block') || scope);
          });
          document.querySelectorAll('.timeline-group').forEach((group) => {
            if (!group.querySelector('[data-video-card]')) fadeRemove(group);
          });
          const tabCount = document.querySelector('[data-page-tab="subscriptions"] .count');
          if (tabCount) {
            const next = Math.max(0, Number(tabCount.textContent || '0') - 1);
            tabCount.textContent = String(next);
          }
          const timeline = document.querySelector('.timeline[data-section-kind="subscription"]');
          if (timeline && !timeline.querySelector('[data-video-card]')) {
            timeline.outerHTML = '<p class="empty-copy">No videos from your subscriptions yet. <button class="link-action" type="button" data-feed-refresh>Sync now</button></p>';
          }
          /* Backfill the home feed so removed cards do not leave a short page. */
          if (removed && feedContainer && feedHasMore) loadFeedPage();
        }, 260);
      }

      /* details/summary menus: keep aria-expanded in step with the open state.
         The toggle event fires for native toggles and for programmatic closes
         (Escape, outside clicks), so one capture listener covers every path.
         Close-on-Escape itself already lives in the keydown handler below. */
      document.addEventListener('toggle', (event) => {
        const menu = event.target && event.target.closest ? event.target.closest('.yt-menu') : null;
        if (!menu) return;
        const menuSummary = menu.querySelector('summary');
        if (menuSummary) menuSummary.setAttribute('aria-expanded', menu.open ? 'true' : 'false');
      }, true);
      document.querySelectorAll('.yt-menu > summary:not([aria-expanded])').forEach((item) => {
        item.setAttribute('aria-expanded', item.closest('.yt-menu').open ? 'true' : 'false');
      });

      document.addEventListener('click', async (event) => {
        const summary = event.target.closest('summary');
        if (summary && summary.closest('.yt-menu')) {
          const current = summary.closest('.yt-menu');
          closeMenus(current);
          window.requestAnimationFrame(() => positionMenu(current));
          event.stopPropagation();
          return;
        }

        /* The empty-feed "Clear filter" action: back to All without a page load. */
        const clearFilter = event.target.closest('[data-clear-filter]');
        if (clearFilter) {
          event.preventDefault();
          setActiveFilter('all');
          if (feedContainer) feedContainer.dataset.nextPage = '0';
          feedHasMore = true;
          await loadFeedPage({ reset: true, filter: 'all' });
          return;
        }

        const filterChip = event.target.closest('[data-filter]');
        if (filterChip) {
          event.preventDefault();
          const filter = filterChip.dataset.filter || 'all';
          setActiveFilter(filter);
          if (feedContainer) feedContainer.dataset.nextPage = '0';
          feedHasMore = true;
          await loadFeedPage({ reset: true, filter });
          return;
        }

        const openDialog = event.target.closest('[data-open-feed-dialog]');
        if (openDialog) {
          event.preventDefault();
          if (dialog && typeof dialog.showModal === 'function') dialog.showModal();
          return;
        }

        const openSettingsDialog = event.target.closest('[data-open-settings-dialog]');
        if (openSettingsDialog) {
          event.preventDefault();
          closeMenus();
          if (settingsDialog && typeof settingsDialog.showModal === 'function') settingsDialog.showModal();
          return;
        }

        const closeDialog = event.target.closest('[data-close-dialog]');
        if (closeDialog) {
          event.preventDefault();
          const activeDialog = closeDialog.closest('dialog');
          if (activeDialog) activeDialog.close();
          return;
        }

        const retryButton = event.target.closest('[data-retry-feed]');
        if (retryButton) {
          event.preventDefault();
          await loadFeedPage();
          return;
        }

        const deleteCollection = event.target.closest('[data-delete-collection]');
        if (deleteCollection) {
          event.preventDefault();
          const collectionId = deleteCollection.dataset.deleteCollection;
          if (!collectionId || !window.confirm('Delete this collection?')) return;
          deleteCollection.disabled = true;
          try {
            const response = await fetch(`/api/subscription-collections/${encodeURIComponent(collectionId)}`, { method: 'DELETE' });
            if (!response.ok) throw new Error('delete failed');
            showToast('Collection deleted');
            if (deleteCollection.dataset.redirect) {
              window.location.href = deleteCollection.dataset.redirect;
              return;
            }
            deleteCollection.closest('[data-collection-card]')?.remove();
          } catch (error) {
            showToast('Could not delete collection');
          } finally {
            deleteCollection.disabled = false;
          }
          return;
        }

        const deletePlaylist = event.target.closest('[data-delete-playlist]');
        if (deletePlaylist) {
          event.preventDefault();
          const playlistId = deletePlaylist.dataset.deletePlaylist;
          if (!playlistId || !window.confirm('Delete this playlist?')) return;
          deletePlaylist.disabled = true;
          try {
            const response = await fetch(`/api/playlists/${encodeURIComponent(playlistId)}`, { method: 'DELETE' });
            if (!response.ok) throw new Error('delete failed');
            showToast('Playlist deleted');
            if (deletePlaylist.dataset.redirect) {
              window.location.href = deletePlaylist.dataset.redirect;
              return;
            }
            deletePlaylist.closest('[data-playlist-card]')?.remove();
          } catch (error) {
            showToast('Could not delete playlist');
          } finally {
            deletePlaylist.disabled = false;
          }
          return;
        }

        const removeItem = event.target.closest('[data-remove-playlist-item]');
        if (removeItem) {
          event.preventDefault();
          const videoId = removeItem.dataset.removePlaylistItem;
          const listId = removeItem.dataset.playlistId;
          if (!videoId || !listId) return;
          removeItem.disabled = true;
          try {
            const response = await fetch(`/api/playlists/${encodeURIComponent(listId)}/items/${encodeURIComponent(videoId)}`, { method: 'DELETE' });
            if (!response.ok) throw new Error('remove failed');
            removeItem.closest('[data-playlist-row]')?.remove();
            showToast('Removed from playlist');
          } catch (error) {
            showToast('Could not remove video');
          } finally {
            removeItem.disabled = false;
          }
          return;
        }

        const channelCollections = event.target.closest('[data-channel-collections]');
        if (channelCollections) {
          event.preventDefault();
          openChannelCollections(channelCollections.dataset.channelCollections, channelCollections.dataset.channelTitle || '');
          return;
        }

        const toggleCollection = event.target.closest('[data-toggle-collection]');
        if (toggleCollection) {
          event.preventDefault();
          await toggleChannelCollection(toggleCollection);
          return;
        }

        const newCollection = event.target.closest('[data-new-collection]');
        if (newCollection) {
          event.preventDefault();
          openCollectionEditor(null, '', '');
          return;
        }

        const editCollection = event.target.closest('[data-edit-collection]');
        if (editCollection) {
          event.preventDefault();
          closeMenus();
          openCollectionEditor(editCollection.dataset.editCollection, editCollection.dataset.collectionName || '', editCollection.dataset.collectionDescription || '');
          return;
        }

        const addChannels = event.target.closest('[data-add-channels]');
        if (addChannels) {
          event.preventDefault();
          closeMenus();
          openChannelPicker(addChannels.dataset.addChannels, addChannels.dataset.collectionName || '');
          return;
        }

        const toggleMember = event.target.closest('[data-toggle-member]');
        if (toggleMember) {
          event.preventDefault();
          await toggleCollectionMember(toggleMember);
          return;
        }

        const removeMember = event.target.closest('[data-remove-collection-member]');
        if (removeMember) {
          event.preventDefault();
          const subscriptionId = removeMember.dataset.removeCollectionMember;
          const listId = removeMember.dataset.collectionId;
          if (!subscriptionId || !listId) return;
          removeMember.disabled = true;
          try {
            const response = await fetch(`/api/subscription-collections/${encodeURIComponent(listId)}/members/${encodeURIComponent(subscriptionId)}`, { method: 'DELETE' });
            if (!response.ok) throw new Error('remove failed');
            removeMember.closest('[data-member-card]')?.remove();
            showToast('Removed from collection');
          } catch (error) {
            showToast('Could not remove channel');
          } finally {
            removeMember.disabled = false;
          }
          return;
        }

        const newPlaylist = event.target.closest('[data-new-playlist]');
        if (newPlaylist) {
          event.preventDefault();
          openPlaylistEditor(null, '', '');
          return;
        }

        const editPlaylist = event.target.closest('[data-edit-playlist]');
        if (editPlaylist) {
          event.preventDefault();
          closeMenus();
          openPlaylistEditor(editPlaylist.dataset.editPlaylist, editPlaylist.dataset.playlistName || '', editPlaylist.dataset.playlistDescription || '');
          return;
        }

        const saveToPlaylist = event.target.closest('[data-save-to-playlist]');
        if (saveToPlaylist) {
          event.preventDefault();
          closeMenus();
          openPlaylistPicker(saveToPlaylist.dataset.saveToPlaylist, saveToPlaylist.dataset.videoTitle || '');
          return;
        }

        const pickerRow = event.target.closest('[data-picker-playlist]');
        if (pickerRow) {
          event.preventDefault();
          await addToPlaylist(pickerRow.dataset.pickerPlaylist);
          return;
        }

        const copyButton = event.target.closest('[data-copy-url]');
        if (copyButton) {
          event.preventDefault();
          const rawUrl = copyButton.dataset.copyUrl;
          if (!rawUrl) return;
          const absoluteUrl = new URL(rawUrl, window.location.origin).toString();
          try {
            await navigator.clipboard.writeText(absoluteUrl);
            showToast('Link copied');
          } catch (error) {
            showToast(absoluteUrl);
          } finally {
            const menu = copyButton.closest('.yt-menu');
            if (menu) menu.removeAttribute('open');
          }
          return;
        }

        const subscribeButton = event.target.closest('[data-subscribe-feed], [data-unsubscribe-feed]');
        if (subscribeButton) {
          const feedUrl = subscribeButton.dataset.feedUrl || '';
          const channelId = subscribeButton.dataset.channelId || '';
          const title = subscribeButton.dataset.title || '';
          if (!feedUrl && !channelId) return;
          const unsubscribe = subscribeButton.hasAttribute('data-unsubscribe-feed');
          if (unsubscribe && !window.confirm(`Unsubscribe from ${title || 'this channel'}?`)) return;
          subscribeButton.disabled = true;
          try {
            const response = await fetch('/api/subscriptions/freshrss', {
              method: unsubscribe ? 'DELETE' : 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ feed_url: feedUrl, youtube_channel_id: channelId, title })
            });
            if (!response.ok) throw new Error('subscription change failed');
            subscribeButton.classList.toggle('subscribed', !unsubscribe);
            const label = subscribeButton.querySelector('span');
            if (label) label.textContent = unsubscribe ? 'Subscribe' : 'Subscribed';
            /* Reveals or hides the notification bell that is already sitting in the
               group, so subscribing in place matches what a reload would render. */
            const group = subscribeButton.closest('[data-subscribe-group]');
            if (group) group.classList.toggle('is-subscribed', !unsubscribe);
            const swap = subscribeButton.querySelector('.icon');
            if (swap && group) swap.outerHTML = unsubscribe ? PLUS_ICON : CHECK_ICON;
            if (unsubscribe) {
              subscribeButton.removeAttribute('data-unsubscribe-feed');
              subscribeButton.setAttribute('data-subscribe-feed', '');
              subscribeButton.setAttribute('aria-label', `Subscribe to ${title || 'channel'}`);
              /* The videos stay in the database, so the feed has to be told the
                 channel is gone instead of waiting for the next page load. */
              dropUnsubscribedChannel(channelId, title);
              showToast(`Unsubscribed from ${title || 'this channel'}`, {
                label: 'Undo',
                onClick: async () => {
                  try {
                    const undo = await fetch('/api/subscriptions/freshrss', {
                      method: 'POST',
                      headers: { 'Content-Type': 'application/json' },
                      body: JSON.stringify({ feed_url: feedUrl, youtube_channel_id: channelId, title })
                    });
                    if (!undo.ok) throw new Error('resubscribe failed');
                    window.location.reload();
                  } catch (error) {
                    showToast('Could not resubscribe');
                  }
                }
              });
            } else {
              subscribeButton.removeAttribute('data-subscribe-feed');
              subscribeButton.setAttribute('data-unsubscribe-feed', '');
              subscribeButton.setAttribute('aria-label', `Unsubscribe from ${title || 'channel'}`);
              showToast('Subscribed');
            }
            refreshSubscriptionNav();
          } catch (error) {
            showToast(unsubscribe ? 'Could not unsubscribe' : 'Could not subscribe');
          } finally {
            subscribeButton.disabled = false;
          }
          return;
        }

        const descriptionButton = event.target.closest('[data-description-toggle]');
        if (descriptionButton) {
          const box = descriptionButton.closest('[data-description-box]');
          if (!box) return;
          const collapsed = box.classList.toggle('is-collapsed');
          descriptionButton.textContent = collapsed ? (descriptionButton.dataset.moreLabel || 'More') : (descriptionButton.dataset.lessLabel || 'Show less');
          return;
        }

        const commentVote = event.target.closest('[data-comment-vote]');
        if (commentVote) {
          event.preventDefault();
          const box = commentVote.closest('[data-comments-for]');
          const videoId = box ? box.dataset.commentsFor : '';
          const commentId = commentVote.dataset.commentId || '';
          if (!videoId || !commentId) return;
          const pressed = commentVote.getAttribute('aria-pressed') === 'true';
          const reaction = pressed ? '' : commentVote.dataset.commentVote;
          /* Paint first: a vote on a comment should feel instant, and the worst case is
             the button snapping back when the write fails. */
          const pair = commentVote.parentElement.querySelectorAll('[data-comment-vote]');
          pair.forEach((button) => {
            const on = !!reaction && button.dataset.commentVote === reaction;
            button.classList.toggle('is-selected', on);
            button.setAttribute('aria-pressed', on ? 'true' : 'false');
          });
          if (reaction) {
            /* Restarted deliberately: without clearing the class first, voting the same
               way twice in a row would not replay the animation. */
            commentVote.classList.remove('is-bumped');
            void commentVote.offsetWidth;
            commentVote.classList.add('is-bumped');
            window.setTimeout(() => commentVote.classList.remove('is-bumped'), 420);
          }
          try {
            const response = await fetch('/api/comments/reaction', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ youtube_video_id: videoId, comment_id: commentId, reaction })
            });
            if (!response.ok) throw new Error('save failed');
          } catch (error) {
            pair.forEach((button) => {
              const on = pressed && button === commentVote;
              button.classList.toggle('is-selected', on);
              button.setAttribute('aria-pressed', on ? 'true' : 'false');
            });
            showToast('Could not save that');
          }
          return;
        }

        /* A timestamp anywhere — description, comment, transcript — seeks the player. */
        const seekButton = event.target.closest('[data-seek]');
        if (seekButton) {
          event.preventDefault();
          const raw = seekButton.dataset.seek || '';
          const seconds = /^\\d+$/.test(raw)
            ? Number(raw)
            : raw.split(':').reduce((total, part) => total * 60 + Number(part || 0), 0);
          const player = document.querySelector('.player-frame');
          if (player && player.contentWindow && isFinite(seconds)) {
            player.contentWindow.postMessage({ type: 'openfeed:seek', seconds }, window.location.origin);
            /* A timestamp deep in the description or a comment is usually off-screen, so
               seeking without moving looks like nothing happened. */
            const stage = document.querySelector('.watch-stage') || player;
            stage.scrollIntoView({ behavior: 'smooth', block: 'start' });
          }
          return;
        }

        const feedbackButton = event.target.closest('[data-feedback]');
        if (feedbackButton) {
          const videoId = feedbackButton.dataset.videoId;
          let eventType = feedbackButton.dataset.feedback;
          if (!videoId || !eventType) return;
          if ((eventType === 'like' || eventType === 'dislike') && feedbackButton.getAttribute('aria-pressed') === 'true') {
            eventType = 'clear_reaction';
          }
          feedbackButton.disabled = true;
          try {
            const card = feedbackButton.closest('[data-video-card]') || document.querySelector(`[data-video-card][data-video-id="${safeCss(videoId)}"]`);
            const response = await fetch('/api/feedback', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ youtube_video_id: videoId, event_type: eventType, payload: { source: 'ui_menu', shown_reason: card?.dataset.source || '', topics: card?.dataset.topics || '' } })
            });
            if (!response.ok) throw new Error('feedback failed');
            const data = await response.json();
            updateReactionButtons(videoId, data.feedback?.reaction || '');
            if (eventType === 'hide' || eventType === 'not_interested') {
              const cards = Array.from(document.querySelectorAll(`[data-video-card][data-video-id="${safeCss(videoId)}"]`));
              cards.forEach((item) => item.hidden = true);
              showToast('Removed from recommendations', {
                label: 'Undo',
                onClick: async () => {
                  try {
                    const undo = await fetch('/api/feedback', {
                      method: 'POST',
                      headers: { 'Content-Type': 'application/json' },
                      body: JSON.stringify({ youtube_video_id: videoId, event_type: 'undo_hide', payload: { source: 'undo_toast' } })
                    });
                    if (!undo.ok) throw new Error('undo failed');
                    cards.forEach((item) => item.hidden = false);
                    showToast('Restored');
                  } catch (error) {
                    showToast('Could not undo');
                  }
                }
              });
            } else {
              showToast(eventType === 'clear_reaction' ? 'Reaction cleared' : 'Saved');
            }
          } catch (error) {
            showToast('Could not save');
          } finally {
            feedbackButton.disabled = false;
            const menu = feedbackButton.closest('.yt-menu');
            if (menu) menu.removeAttribute('open');
          }
          return;
        }

        if (!event.target.closest('.yt-menu')) closeMenus();
      });

      document.addEventListener('keydown', (event) => {
        if (event.key === 'Escape') closeMenus();
      });

      window.addEventListener('resize', () => {
        document.querySelectorAll('.yt-menu[open]').forEach((menu) => positionMenu(menu));
      });

      document.querySelectorAll('.tab').forEach((button) => {
        button.addEventListener('click', () => {
          document.querySelectorAll('.tab').forEach((tab) => tab.classList.toggle('active', tab === button));
          document.querySelectorAll('.tab-panel').forEach((panel) => panel.classList.toggle('active', panel.id === `tab-${button.dataset.tab}`));
        });
      });

      /* ---- playlists: create / rename / save-to ---- */
      const playlistEditDialog = document.getElementById('playlist-edit-dialog');
      const playlistEditForm = document.getElementById('playlist-edit-form');
      const playlistPicker = document.getElementById('playlist-picker-dialog');
      let editingPlaylistId = null;
      let pendingVideoId = null;

      function openPlaylistEditor(playlistId, name, description) {
        if (!playlistEditDialog || !playlistEditForm) return;
        editingPlaylistId = playlistId || null;
        playlistEditForm.querySelector('[name="name"]').value = name || '';
        playlistEditForm.querySelector('[name="description"]').value = description || '';
        playlistEditDialog.querySelector('#playlist-edit-title').textContent = playlistId ? 'Rename playlist' : 'New playlist';
        const submit = playlistEditForm.querySelector('[data-playlist-submit]');
        if (submit) submit.textContent = playlistId ? 'Save' : 'Create';
        const status = playlistEditForm.querySelector('[data-playlist-status]');
        if (status) status.textContent = '';
        if (typeof playlistEditDialog.showModal === 'function') playlistEditDialog.showModal();
        playlistEditForm.querySelector('[name="name"]').focus();
      }

      playlistEditForm?.addEventListener('submit', async (event) => {
        event.preventDefault();
        const data = new FormData(playlistEditForm);
        const payload = {
          name: String(data.get('name') || '').trim(),
          description: String(data.get('description') || '').trim() || null
        };
        if (!payload.name) return;
        const submit = playlistEditForm.querySelector('[data-playlist-submit]');
        const status = playlistEditForm.querySelector('[data-playlist-status]');
        if (submit) submit.disabled = true;
        if (status) status.textContent = 'Saving…';
        try {
          const url = editingPlaylistId ? `/api/playlists/${encodeURIComponent(editingPlaylistId)}` : '/api/playlists';
          const response = await fetch(url, {
            method: editingPlaylistId ? 'PATCH' : 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
          });
          if (!response.ok) throw new Error('save failed');
          playlistEditDialog.close();
          showToast(editingPlaylistId ? 'Playlist saved' : 'Playlist created');
          window.location.reload();
        } catch (error) {
          if (status) status.textContent = 'Could not save';
        } finally {
          if (submit) submit.disabled = false;
        }
      });

      function renderPickerRows(items) {
        const list = playlistPicker?.querySelector('[data-picker-list]');
        if (!list) return;
        list.replaceChildren();
        if (!items.length) {
          const empty = document.createElement('p');
          empty.className = 'picker-empty';
          empty.textContent = 'No playlists yet — create one below.';
          list.appendChild(empty);
          return;
        }
        items.forEach((item) => {
          const button = document.createElement('button');
          button.type = 'button';
          button.className = 'picker-row';
          button.dataset.pickerPlaylist = item.id;
          const icon = document.createElement('span');
          icon.innerHTML = PLAYLIST_ICON;
          const copy = document.createElement('span');
          const title = document.createElement('span');
          title.textContent = item.name || 'Untitled playlist';
          const meta = document.createElement('small');
          const count = Number(item.video_count || 0);
          meta.textContent = `${count} video${count === 1 ? '' : 's'}`;
          copy.append(title, meta);
          const plus = document.createElement('span');
          plus.innerHTML = PLUS_ICON;
          button.append(icon, copy, plus);
          list.appendChild(button);
        });
      }

      async function openPlaylistPicker(videoId, videoTitle) {
        if (!playlistPicker || !videoId) return;
        pendingVideoId = videoId;
        const subtitle = playlistPicker.querySelector('[data-picker-subtitle]');
        if (subtitle) subtitle.textContent = videoTitle ? `Saving “${videoTitle}”` : 'Choose a playlist for this video.';
        const list = playlistPicker.querySelector('[data-picker-list]');
        if (list) list.innerHTML = '<p class="picker-empty">Loading playlists…</p>';
        if (typeof playlistPicker.showModal === 'function') playlistPicker.showModal();
        try {
          const response = await fetch('/api/playlists');
          if (!response.ok) throw new Error('load failed');
          const data = await response.json();
          renderPickerRows(data.items || []);
        } catch (error) {
          if (list) list.innerHTML = '<p class="picker-empty">Could not load playlists.</p>';
        }
      }

      async function addToPlaylist(playlistId) {
        if (!playlistId || !pendingVideoId) return;
        try {
          const response = await fetch(`/api/playlists/${encodeURIComponent(playlistId)}/items`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ youtube_video_id: pendingVideoId })
          });
          if (!response.ok) throw new Error('add failed');
          playlistPicker?.close();
          showToast('Saved to playlist');
        } catch (error) {
          showToast('Could not save to playlist');
        }
      }

      playlistPicker?.querySelector('[data-picker-create]')?.addEventListener('submit', async (event) => {
        event.preventDefault();
        const form = event.currentTarget;
        const input = form.querySelector('[name="name"]');
        const name = String(input?.value || '').trim();
        if (!name) return;
        const button = form.querySelector('button[type="submit"]');
        if (button) button.disabled = true;
        try {
          const response = await fetch('/api/playlists', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name, description: null })
          });
          if (!response.ok) throw new Error('create failed');
          const data = await response.json();
          if (input) input.value = '';
          await addToPlaylist(data.playlist?.id);
        } catch (error) {
          showToast('Could not create playlist');
        } finally {
          if (button) button.disabled = false;
        }
      });


      /* ---- add a channel to collections (from the channel page) ---- */
      const channelCollectionsDialog = document.getElementById('channel-collections-dialog');
      let activeSubscriptionId = null;
      let membershipDirty = false;

      function renderCollectionRows(collections, memberIds) {
        const list = channelCollectionsDialog?.querySelector('[data-channel-collections-list]');
        if (!list) return;
        list.replaceChildren();
        if (!collections.length) {
          const empty = document.createElement('p');
          empty.className = 'picker-empty';
          empty.textContent = 'No collections yet — create one below.';
          list.appendChild(empty);
          return;
        }
        collections.forEach((item) => {
          const isMember = memberIds.has(String(item.id));
          const button = document.createElement('button');
          button.type = 'button';
          button.className = `picker-row${isMember ? ' is-member' : ''}`;
          button.dataset.toggleCollection = item.id;
          button.dataset.memberState = isMember ? 'in' : 'out';
          const icon = document.createElement('span');
          icon.innerHTML = COLLECTIONS_ICON;
          const copy = document.createElement('span');
          const label = document.createElement('span');
          label.textContent = item.name || 'Untitled collection';
          const meta = document.createElement('small');
          const count = Number(item.subscription_count || 0);
          meta.textContent = isMember ? 'Channel is in this collection' : `${count} channel${count === 1 ? '' : 's'}`;
          copy.append(label, meta);
          const check = document.createElement('span');
          check.className = 'picker-check';
          check.innerHTML = isMember ? CHECK_ICON : PLUS_ICON;
          button.append(icon, copy, check);
          list.appendChild(button);
        });
      }

      async function openChannelCollections(subscriptionId, channelTitle) {
        if (!channelCollectionsDialog || !subscriptionId) return;
        activeSubscriptionId = subscriptionId;
        const subtitle = channelCollectionsDialog.querySelector('[data-channel-collections-subtitle]');
        if (subtitle) subtitle.textContent = channelTitle ? `Collections for “${channelTitle}”` : 'Choose the collections this channel belongs to.';
        const list = channelCollectionsDialog.querySelector('[data-channel-collections-list]');
        if (list) list.innerHTML = '<p class="picker-empty">Loading collections…</p>';
        if (typeof channelCollectionsDialog.showModal === 'function') channelCollectionsDialog.showModal();
        try {
          const [collectionsResponse, membershipResponse] = await Promise.all([
            fetch('/api/subscription-collections'),
            fetch('/api/subscription-collections/memberships')
          ]);
          if (!collectionsResponse.ok || !membershipResponse.ok) throw new Error('load failed');
          const collectionsData = await collectionsResponse.json();
          const membershipData = await membershipResponse.json();
          const memberIds = new Set((membershipData.items || {})[subscriptionId] || []);
          renderCollectionRows(collectionsData.items || [], memberIds);
        } catch (error) {
          if (list) list.innerHTML = '<p class="picker-empty">Could not load collections.</p>';
        }
      }

      async function toggleChannelCollection(button) {
        if (!activeSubscriptionId) return;
        const collectionId = button.dataset.toggleCollection;
        const isMember = button.dataset.memberState === 'in';
        button.disabled = true;
        try {
          const base = `/api/subscription-collections/${encodeURIComponent(collectionId)}/members`;
          const response = isMember
            ? await fetch(`${base}/${encodeURIComponent(activeSubscriptionId)}`, { method: 'DELETE' })
            : await fetch(base, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ subscription_id: activeSubscriptionId })
              });
          if (!response.ok) throw new Error('toggle failed');
          const nowMember = !isMember;
          button.dataset.memberState = nowMember ? 'in' : 'out';
          button.classList.toggle('is-member', nowMember);
          const check = button.querySelector('.picker-check');
          if (check) check.innerHTML = nowMember ? CHECK_ICON : PLUS_ICON;
          const meta = button.querySelector('small');
          if (meta && nowMember) meta.textContent = 'Channel is in this collection';
          membershipDirty = true;
          showToast(nowMember ? 'Added to collection' : 'Removed from collection');
        } catch (error) {
          showToast('Could not update collection');
        } finally {
          button.disabled = false;
        }
      }

      channelCollectionsDialog?.querySelector('[data-collection-quick-create]')?.addEventListener('submit', async (event) => {
        event.preventDefault();
        const form = event.currentTarget;
        const input = form.querySelector('[name="name"]');
        const name = String(input?.value || '').trim();
        if (!name || !activeSubscriptionId) return;
        const button = form.querySelector('button[type="submit"]');
        if (button) button.disabled = true;
        try {
          const created = await fetch('/api/subscription-collections', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name, description: null })
          });
          if (!created.ok) throw new Error('create failed');
          const data = await created.json();
          const added = await fetch(`/api/subscription-collections/${encodeURIComponent(data.collection.id)}/members`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ subscription_id: activeSubscriptionId })
          });
          if (!added.ok) throw new Error('add failed');
          if (input) input.value = '';
          membershipDirty = true;
          showToast('Collection created');
          await openChannelCollections(activeSubscriptionId, '');
        } catch (error) {
          showToast('Could not create collection');
        } finally {
          if (button) button.disabled = false;
        }
      });

      /* ---- collections: create / rename / membership ---- */
      const collectionEditDialog = document.getElementById('collection-edit-dialog');
      const collectionEditForm = document.getElementById('collection-edit-form');
      const channelsDialog = document.getElementById('collection-channels-dialog');
      let editingCollectionId = null;
      let activeCollectionId = null;

      function openCollectionEditor(collectionId, name, description) {
        if (!collectionEditDialog || !collectionEditForm) return;
        editingCollectionId = collectionId || null;
        collectionEditForm.querySelector('[name="name"]').value = name || '';
        collectionEditForm.querySelector('[name="description"]').value = description || '';
        collectionEditDialog.querySelector('#collection-edit-title').textContent = collectionId ? 'Rename collection' : 'New collection';
        const submit = collectionEditForm.querySelector('[data-collection-submit]');
        if (submit) submit.textContent = collectionId ? 'Save' : 'Create';
        const status = collectionEditForm.querySelector('[data-collection-status]');
        if (status) status.textContent = '';
        if (typeof collectionEditDialog.showModal === 'function') collectionEditDialog.showModal();
        collectionEditForm.querySelector('[name="name"]').focus();
      }

      collectionEditForm?.addEventListener('submit', async (event) => {
        event.preventDefault();
        const data = new FormData(collectionEditForm);
        const payload = {
          name: String(data.get('name') || '').trim(),
          description: String(data.get('description') || '').trim() || null
        };
        if (!payload.name) return;
        const submit = collectionEditForm.querySelector('[data-collection-submit]');
        const status = collectionEditForm.querySelector('[data-collection-status]');
        if (submit) submit.disabled = true;
        if (status) status.textContent = 'Saving…';
        try {
          const url = editingCollectionId
            ? `/api/subscription-collections/${encodeURIComponent(editingCollectionId)}`
            : '/api/subscription-collections';
          const response = await fetch(url, {
            method: editingCollectionId ? 'PATCH' : 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
          });
          if (!response.ok) throw new Error('save failed');
          collectionEditDialog.close();
          showToast(editingCollectionId ? 'Collection saved' : 'Collection created');
          window.location.reload();
        } catch (error) {
          if (status) status.textContent = 'Could not save';
        } finally {
          if (submit) submit.disabled = false;
        }
      });

      function renderChannelRows(subscriptions, memberIds) {
        const list = channelsDialog?.querySelector('[data-channels-list]');
        if (!list) return;
        list.replaceChildren();
        if (!subscriptions.length) {
          const empty = document.createElement('p');
          empty.className = 'picker-empty';
          empty.textContent = 'No subscriptions yet.';
          list.appendChild(empty);
          return;
        }
        subscriptions.forEach((item) => {
          const isMember = memberIds.has(String(item.id));
          const button = document.createElement('button');
          button.type = 'button';
          button.className = `picker-row${isMember ? ' is-member' : ''}`;
          button.dataset.toggleMember = item.id;
          button.dataset.memberState = isMember ? 'in' : 'out';
          button.dataset.searchKey = String(item.title || '').toLowerCase();
          const avatar = document.createElement('span');
          avatar.className = 'avatar';
          const title = String(item.title || 'Untitled');
          avatar.textContent = title.trim().slice(0, 2).toUpperCase();
          const copy = document.createElement('span');
          const label = document.createElement('span');
          label.textContent = title;
          const meta = document.createElement('small');
          meta.textContent = isMember ? 'In this collection' : 'Not in this collection';
          copy.append(label, meta);
          const check = document.createElement('span');
          check.className = 'picker-check';
          check.innerHTML = isMember ? CHECK_ICON : PLUS_ICON;
          button.append(avatar, copy, check);
          list.appendChild(button);
        });
      }

      async function openChannelPicker(collectionId, collectionName) {
        if (!channelsDialog || !collectionId) return;
        activeCollectionId = collectionId;
        const subtitle = channelsDialog.querySelector('[data-channels-subtitle]');
        if (subtitle) subtitle.textContent = collectionName ? `Choose channels for “${collectionName}”` : 'Pick the subscriptions that belong in this collection.';
        const status = channelsDialog.querySelector('[data-channels-status]');
        if (status) status.textContent = '';
        const list = channelsDialog.querySelector('[data-channels-list]');
        if (list) list.innerHTML = '<p class="picker-empty">Loading subscriptions…</p>';
        const filter = channelsDialog.querySelector('[data-channels-filter]');
        if (filter) filter.value = '';
        if (typeof channelsDialog.showModal === 'function') channelsDialog.showModal();
        try {
          const [subsResponse, membersResponse] = await Promise.all([
            fetch('/api/subscriptions'),
            fetch(`/api/subscription-collections/${encodeURIComponent(collectionId)}/subscriptions`)
          ]);
          if (!subsResponse.ok || !membersResponse.ok) throw new Error('load failed');
          const subsData = await subsResponse.json();
          const membersData = await membersResponse.json();
          const memberIds = new Set((membersData.items || []).map((item) => String(item.id)));
          renderChannelRows(subsData.items || [], memberIds);
        } catch (error) {
          if (list) list.innerHTML = '<p class="picker-empty">Could not load subscriptions.</p>';
        }
      }

      async function toggleCollectionMember(button) {
        if (!activeCollectionId) return;
        const subscriptionId = button.dataset.toggleMember;
        const isMember = button.dataset.memberState === 'in';
        const status = channelsDialog?.querySelector('[data-channels-status]');
        button.disabled = true;
        try {
          const base = `/api/subscription-collections/${encodeURIComponent(activeCollectionId)}/members`;
          const response = isMember
            ? await fetch(`${base}/${encodeURIComponent(subscriptionId)}`, { method: 'DELETE' })
            : await fetch(base, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ subscription_id: subscriptionId })
              });
          if (!response.ok) throw new Error('toggle failed');
          const nowMember = !isMember;
          button.dataset.memberState = nowMember ? 'in' : 'out';
          button.classList.toggle('is-member', nowMember);
          const meta = button.querySelector('small');
          if (meta) meta.textContent = nowMember ? 'In this collection' : 'Not in this collection';
          const check = button.querySelector('.picker-check');
          if (check) check.innerHTML = nowMember ? CHECK_ICON : PLUS_ICON;
          if (status) status.textContent = nowMember ? 'Added' : 'Removed';
          collectionsDirty = true;
        } catch (error) {
          if (status) status.textContent = 'Could not update';
        } finally {
          button.disabled = false;
        }
      }

      let collectionsDirty = false;
      channelsDialog?.addEventListener('close', () => {
        if (collectionsDirty) window.location.reload();
      });

      channelsDialog?.querySelector('[data-channels-filter]')?.addEventListener('input', (event) => {
        const needle = String(event.target.value || '').trim().toLowerCase();
        channelsDialog.querySelectorAll('[data-toggle-member]').forEach((row) => {
          row.hidden = Boolean(needle) && !(row.dataset.searchKey || '').includes(needle);
        });
      });


      const preferenceForm = document.getElementById('feed-preferences-form');
      const preferenceStatus = document.getElementById('feed-preferences-status');

      /* ---- personalize feed dialog: live, colour-shifting controls ---- */
      const tuneRoot = document.querySelector('[data-tune-root]');
      function syncTuneVisuals() {
        if (!tuneRoot) return;
        const range = tuneRoot.querySelector('[data-tune-range]');
        if (!range) return;
        const value = Math.max(0, Math.min(100, Number(range.value || 0)));
        const hue = Math.round(190 + value * 1.65);
        tuneRoot.style.setProperty('--tune', String(value));

        const readout = tuneRoot.querySelector('[data-tune-readout]');
        if (readout) readout.textContent = `${Math.round(value)}%`;

        const state = tuneRoot.querySelector('[data-tune-state]');
        if (state) {
          state.textContent = value <= 12 ? 'Comfort zone'
            : value <= 32 ? 'Mostly familiar'
            : value <= 56 ? 'Balanced'
            : value <= 78 ? 'Curious'
            : 'Wide open';
        }

        /* The mix bar is the honest part: it shows what the slider actually does
           to the feed, so the colour change is not just decoration. */
        const prioritize = tuneRoot.querySelector('[name="prioritize_subscriptions"]');
        let fresh = Math.round(18 + value * 0.62);
        if (prioritize && !prioritize.checked) fresh = Math.min(92, fresh + 10);
        const known = 100 - fresh;
        const knownBar = tuneRoot.querySelector('[data-tune-known]');
        const newBar = tuneRoot.querySelector('[data-tune-new]');
        if (knownBar) knownBar.style.width = `${known}%`;
        if (newBar) newBar.style.width = `${fresh}%`;
        const knownPct = tuneRoot.querySelector('[data-tune-known-pct]');
        const newPct = tuneRoot.querySelector('[data-tune-new-pct]');
        if (knownPct) knownPct.textContent = `${known}%`;
        if (newPct) newPct.textContent = `${fresh}%`;
      }

      if (tuneRoot) {
        const range = tuneRoot.querySelector('[data-tune-range]');
        const startSliding = () => tuneRoot.classList.add('is-sliding');
        const stopSliding = () => tuneRoot.classList.remove('is-sliding');
        range?.addEventListener('input', () => { startSliding(); syncTuneVisuals(); });
        range?.addEventListener('change', syncTuneVisuals);
        ['pointerdown', 'keydown', 'focus'].forEach((name) => range?.addEventListener(name, startSliding));
        ['pointerup', 'pointercancel', 'blur', 'keyup'].forEach((name) => range?.addEventListener(name, stopSliding));
        window.addEventListener('pointerup', stopSliding);
        tuneRoot.querySelectorAll('[data-tune-input]').forEach((input) => input.addEventListener('change', syncTuneVisuals));

        tuneRoot.querySelectorAll('[data-topic-input]').forEach((input) => {
          const list = tuneRoot.querySelector(`[data-topic-chips="${input.dataset.topicInput}"]`);
          if (!list) return;
          const render = () => {
            list.replaceChildren();
            [...new Set(parseTopics(input.value))].forEach((topic) => {
              const chip = document.createElement('button');
              chip.type = 'button';
              chip.setAttribute('aria-label', `Remove ${topic}`);
              const text = document.createElement('span');
              text.textContent = topic;
              const cross = document.createElement('em');
              cross.textContent = '\\u00d7';
              chip.append(text, cross);
              chip.addEventListener('click', () => {
                input.value = parseTopics(input.value).filter((item) => item !== topic).join(', ');
                render();
              });
              list.appendChild(chip);
            });
          };
          input.addEventListener('input', render);
          input.renderTopicChips = render;
          render();
        });
        syncTuneVisuals();
      }

      if (preferenceForm) {
        preferenceForm.addEventListener('submit', async (event) => {
          event.preventDefault();
          const formData = new FormData(preferenceForm);
          const payload = {
            preferred_topics: parseTopics(formData.get('preferred_topics')),
            muted_topics: parseTopics(formData.get('muted_topics')),
            prioritize_subscriptions: formData.has('prioritize_subscriptions'),
            exclude_shorts: formData.has('exclude_shorts'),
            discovery_level: Number(formData.get('discovery_level') || 35) / 100
          };
          if (preferenceStatus) preferenceStatus.textContent = 'Saving...';
          try {
            const response = await fetch('/api/feed/preferences', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify(payload)
            });
            if (!response.ok) throw new Error('save failed');
            if (preferenceStatus) preferenceStatus.textContent = 'Saved';
            if (dialog) dialog.close();
            feedHasMore = true;
            if (feedContainer) feedContainer.dataset.nextPage = '0';
            await loadFeedPage({ reset: true, filter: currentFilter() });
            showToast('Feed preferences saved');
          } catch (error) {
            if (preferenceStatus) preferenceStatus.textContent = 'Could not save';
          }
        });
      }

      document.querySelector('[data-reset-feed-preferences]')?.addEventListener('click', async () => {
        if (!preferenceForm) return;
        preferenceForm.querySelector('[name="preferred_topics"]').value = '';
        preferenceForm.querySelector('[name="muted_topics"]').value = '';
        preferenceForm.querySelector('[name="prioritize_subscriptions"]').checked = true;
        preferenceForm.querySelector('[name="exclude_shorts"]').checked = false;
        preferenceForm.querySelector('[name="discovery_level"]').value = 35;
        preferenceForm.querySelectorAll('[data-topic-input]').forEach((input) => input.renderTopicChips?.());
        syncTuneVisuals();
        preferenceForm.requestSubmit();
      });

      if (sentinel && feedContainer && 'IntersectionObserver' in window) {
        const observer = new IntersectionObserver((entries) => {
          if (entries.some((entry) => entry.isIntersecting)) loadFeedPage();
        }, { rootMargin: '900px 0px' });
        observer.observe(sentinel);
      }

      /* ---- sidebar collapse ---- */
      /* ---- shorts feed ---- */
      (() => {
        const stage = document.querySelector('[data-shorts-stage]');
        if (!stage) return;
        const viewport = document.querySelector('[data-shorts-viewport]');
        const panel = document.querySelector('[data-shorts-comment-panel]');
        const panelBody = document.querySelector('[data-shorts-comment-body]');
        let activeSlide = null;
        let loading = false;
        let exhausted = false;
        let watchTimer = null;

        const slides = () => [...stage.querySelectorAll('[data-shorts-slide]')];

        /* A small window of players stays mounted rather than only the visible one.
           Mounting on arrival meant every scroll paid for a fresh player page plus the
           first video segments, which is the pause between flicking and seeing anything.
           The window is deliberately small — two ahead, one behind — because each frame
           is a real request to Invidious and preloading the whole deck would both burn
           bandwidth and risk tripping rate limits. */
        const PRELOAD_AHEAD = 2;
        const PRELOAD_BEHIND = 1;

        function mountPlayer(slide, active) {
          const frame = slide.querySelector('[data-shorts-frame]');
          if (!frame) return;
          let iframe = frame.querySelector('iframe');
          if (!iframe) {
            const videoId = slide.dataset.videoId;
            iframe = document.createElement('iframe');
            /* Preloaded frames autoplay=0: they fetch the player and buffer, but stay
               paused until this slide is the one on screen. */
            iframe.src = `/watch/${encodeURIComponent(videoId)}/player?autoplay=${active ? 1 : 0}`;
            iframe.title = slide.dataset.title || 'Short';
            iframe.allow = 'autoplay; fullscreen; picture-in-picture';
            iframe.setAttribute('allowfullscreen', '');
            frame.appendChild(iframe);
          }
          if (active) {
            slide.classList.add('is-playing');
            tell(iframe, 'openfeed:play');
          }
        }

        function tell(iframe, type) {
          try { iframe?.contentWindow?.postMessage({ type }, window.location.origin); } catch (error) { /* not ready yet */ }
        }

        function pausePlayer(slide) {
          const iframe = slide.querySelector('[data-shorts-frame] iframe');
          if (iframe) tell(iframe, 'openfeed:pause');
          slide.classList.remove('is-playing');
        }

        function unmountPlayer(slide) {
          const frame = slide.querySelector('[data-shorts-frame]');
          frame?.querySelector('iframe')?.remove();
          slide.classList.remove('is-playing');
        }

        /* Keep the window populated and evict anything outside it, so memory and the
           number of live players stay bounded however far the user scrolls. */
        function syncWindow(index) {
          const list = slides();
          list.forEach((slide, position) => {
            const distance = position - index;
            const inWindow = distance >= -PRELOAD_BEHIND && distance <= PRELOAD_AHEAD;
            if (!inWindow) { unmountPlayer(slide); return; }
            if (distance === 0) return;
            /* Staggered so arriving at a Short does not fire three player loads at once.
               The immediate neighbour is worth loading now; the one after can wait. */
            const delay = distance === 1 ? 0 : 400 * Math.abs(distance);
            window.setTimeout(() => {
              if (slide.isConnected) mountPlayer(slide, false);
            }, delay);
          });
        }

        function setActive(slide) {
          if (!slide || slide === activeSlide) return;
          if (activeSlide) pausePlayer(activeSlide);
          activeSlide = slide;
          mountPlayer(slide, true);
          syncWindow(slides().indexOf(slide));
          recordShortView(slide.dataset.videoId);
          if (panel && !panel.hidden) loadShortComments(slide.dataset.videoId);
          const url = `/shorts?start=${encodeURIComponent(slide.dataset.videoId)}`;
          history.replaceState(null, '', url);
        }

        /* Watching happens in the viewer, never on a watch page, so without this
           nothing is ever recorded and the same shorts keep resurfacing in
           recommendations. A few seconds active means it was watched; anything
           flicked past is cleared before the timer fires. */
        function recordShortView(videoId) {
          window.clearTimeout(watchTimer);
          if (!videoId) return;
          watchTimer = window.setTimeout(() => {
            fetch('/api/feedback', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ youtube_video_id: videoId, event_type: 'watch_page_open', payload: { source: 'shorts_viewer' } })
            }).catch(() => {});
          }, 3000);
        }

        // The active slide is derived from scroll position rather than an
        // IntersectionObserver: slides are exactly one stage-height tall, so the index is
        // just scrollTop / height, and that stays correct even while snapping.
        function syncActive() {
          const height = stage.clientHeight;
          if (!height) return;
          const list = slides();
          const index = Math.max(0, Math.min(list.length - 1, Math.round(stage.scrollTop / height)));
          setActive(list[index]);
        }

        // A timer rather than requestAnimationFrame: rAF is throttled (or never fires)
        // when the pane is backgrounded, which would strand the active slide.
        let syncTimer = null;
        let settleTimer = null;
        function queueSync() {
          // Leading sync keeps the UI responsive mid-scroll; the trailing one re-reads the
          // position after scroll-snap has settled, which a single sync can miss.
          if (!syncTimer) syncTimer = setTimeout(() => { syncTimer = null; syncActive(); }, 70);
          clearTimeout(settleTimer);
          settleTimer = setTimeout(syncActive, 280);
        }
        function observeAll() { queueSync(); }

        function go(delta) {
          const list = slides();
          const index = Math.max(0, list.indexOf(activeSlide));
          const target = Math.max(0, Math.min(list.length - 1, index + delta));
          // Assigning scrollTop directly: scroll-snap makes smooth behaviour a no-op in
          // this container, and the snap animation supplies the motion anyway.
          stage.scrollTop = target * stage.clientHeight;
          syncActive();
        }

        document.querySelector('[data-shorts-prev]')?.addEventListener('click', () => go(-1));
        document.querySelector('[data-shorts-next]')?.addEventListener('click', () => go(1));

        document.addEventListener('keydown', (event) => {
          // event.target is `document` for programmatic events and has no .matches().
          const target = event.target;
          if (target instanceof Element && target.closest('input, textarea, select, [contenteditable]')) return;
          if (event.key === 'ArrowDown' || event.key === 'j') { event.preventDefault(); go(1); }
          else if (event.key === 'ArrowUp' || event.key === 'k') { event.preventDefault(); go(-1); }
        });

        // Append more Shorts as the feed nears its end.
        async function loadMore() {
          if (loading || exhausted) return;
          loading = true;
          try {
            const offset = Number(stage.dataset.nextOffset || 0);
            const response = await fetch(`/api/shorts?offset=${offset}&limit=8`);
            if (!response.ok) throw new Error('load failed');
            const data = await response.json();
            if (!data.count) { exhausted = true; return; }
            stage.insertAdjacentHTML('beforeend', data.html);
            stage.dataset.nextOffset = String(data.next_offset);
            observeAll();
          } catch (error) {
            exhausted = true;
          } finally {
            loading = false;
          }
        }

        stage.addEventListener('scroll', () => {
          queueSync();
          if (stage.scrollTop + stage.clientHeight * 2.5 >= stage.scrollHeight) loadMore();
        }, { passive: true });
        window.addEventListener('resize', queueSync);
        stage.addEventListener('scrollend', syncActive);

        // tap the poster to start playback without waiting for the observer
        stage.addEventListener('click', (event) => {
          const play = event.target.closest('[data-shorts-play]');
          if (!play) return;
          const slide = play.closest('[data-shorts-slide]');
          setActive(slide);
          mountPlayer(slide);
        });

        async function loadShortComments(videoId) {
          if (!panelBody || !videoId) return;
          panelBody.replaceChildren();
          const loadingNote = document.createElement('p');
          loadingNote.className = 'comments-state';
          loadingNote.textContent = 'Loading comments…';
          panelBody.appendChild(loadingNote);
          try {
            const response = await fetch(`/api/comments/${encodeURIComponent(videoId)}`);
            if (response.status === 403) {
              panelBody.replaceChildren();
              const wrap = document.createElement('div');
              wrap.className = 'shorts-enable';
              const copy = document.createElement('p');
              copy.textContent = 'Comments are turned off. Enable them to read comments here and on watch pages.';
              const button = document.createElement('button');
              button.className = 'primary-action';
              button.type = 'button';
              button.textContent = 'Enable comments';
              button.addEventListener('click', async () => {
                button.disabled = true;
                try {
                  const current = await fetch('/api/feed/preferences').then((r) => r.json());
                  const preferences = current.preferences || {};
                  await fetch('/api/feed/preferences', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                      preferred_topics: preferences.preferred_topics || [],
                      muted_topics: preferences.muted_topics || [],
                      prioritize_subscriptions: preferences.prioritize_subscriptions !== false,
                      exclude_shorts: !!preferences.exclude_shorts,
                      discovery_level: preferences.discovery_level ?? 0.35,
                      show_comments: true
                    })
                  });
                  loadShortComments(videoId);
                } catch (error) {
                  button.disabled = false;
                }
              });
              wrap.append(copy, button);
              panelBody.appendChild(wrap);
              return;
            }
            if (!response.ok) throw new Error('failed');
            const data = await response.json();
            const items = data.items || [];
            panelBody.replaceChildren();
            if (!items.length) {
              const empty = document.createElement('p');
              empty.className = 'comments-state';
              empty.textContent = 'No comments on this Short.';
              panelBody.appendChild(empty);
              return;
            }
            items.forEach((item) => panelBody.appendChild(renderComment(item)));
          } catch (error) {
            panelBody.replaceChildren();
            const failed = document.createElement('p');
            failed.className = 'comments-state';
            failed.textContent = 'Comments could not be loaded.';
            panelBody.appendChild(failed);
          }
        }

        document.addEventListener('click', (event) => {
          const open = event.target.closest('[data-shorts-comments]');
          if (open) {
            event.preventDefault();
            panel.hidden = false;
            viewport.classList.add('comments-open');
            loadShortComments(open.dataset.shortsComments);
            return;
          }
          if (event.target.closest('[data-shorts-comments-close]')) {
            panel.hidden = true;
            viewport.classList.remove('comments-open');
          }
        });

        syncActive();
      })();

      /* ---- notifications ---- */
      const notificationsMenu = document.querySelector('[data-notifications]');
      let notificationsLoaded = false;

      function renderNotification(item) {
        const row = document.createElement('a');
        row.className = item.unread ? 'notification-row unread' : 'notification-row';
        row.href = `/watch/${encodeURIComponent(item.youtube_video_id)}`;

        const avatar = document.createElement('span');
        avatar.className = item.channel_avatar_url ? 'avatar has-image' : 'avatar';
        const initials = document.createElement('span');
        initials.className = 'avatar-initials';
        initials.textContent = (item.channel_title || '?').slice(0, 2).toUpperCase();
        avatar.appendChild(initials);
        if (item.channel_avatar_url) {
          const img = document.createElement('img');
          img.src = item.channel_avatar_url;
          img.loading = 'lazy';
          img.alt = '';
          img.addEventListener('error', () => { avatar.classList.remove('has-image'); img.remove(); });
          avatar.appendChild(img);
        }

        const copy = document.createElement('span');
        copy.className = 'notification-copy';
        const who = document.createElement('strong');
        who.textContent = `${item.channel_title} uploaded`;
        /* Unread is not color-only: a screen-reader "New" prefix backs the tint. */
        if (item.unread) {
          const flag = document.createElement('span');
          flag.className = 'sr-only';
          flag.textContent = 'New: ';
          who.prepend(flag);
        }
        const what = document.createElement('span');
        what.textContent = item.title;
        const when = document.createElement('small');
        when.textContent = [item.published_text, item.duration_text].filter(Boolean).join(' · ');
        copy.append(who, what, when);

        const thumbWrap = document.createElement('span');
        thumbWrap.className = 'notification-thumb';
        if (item.thumbnail_url) {
          const t = document.createElement('img');
          t.src = item.thumbnail_url;
          t.loading = 'lazy';
          t.alt = '';
          t.addEventListener('error', () => t.remove());
          thumbWrap.appendChild(t);
        }

        row.append(avatar, copy, thumbWrap);
        return row;
      }

      async function loadNotifications() {
        const list = notificationsMenu?.querySelector('[data-notification-list]');
        const summary = notificationsMenu?.querySelector('[data-notification-summary]');
        if (!list) return;
        try {
          const response = await fetch('/api/notifications');
          if (!response.ok) throw new Error('load failed');
          const data = await response.json();
          const items = data.items || [];
          list.replaceChildren();
          if (!items.length) {
            const empty = document.createElement('p');
            empty.className = 'notification-empty';
            empty.textContent = 'Nothing new from your subscriptions yet.';
            list.appendChild(empty);
          } else {
            items.forEach((item) => list.appendChild(renderNotification(item)));
          }
          if (summary) summary.textContent = data.unread ? `${data.unread} new` : 'From your subscriptions';
          // Opening the panel counts as seeing them; clear the badge.
          await fetch('/api/notifications/seen', { method: 'POST' });
          const badge = document.querySelector('[data-bell-badge]');
          if (badge) { badge.hidden = true; badge.textContent = ''; }
        } catch (error) {
          list.replaceChildren();
          const failed = document.createElement('p');
          failed.className = 'notification-empty';
          failed.textContent = 'Could not load notifications.';
          list.appendChild(failed);
        }
      }

      notificationsMenu?.addEventListener('toggle', () => {
        if (notificationsMenu.open && !notificationsLoaded) {
          notificationsLoaded = true;
          loadNotifications();
        }
      });

      /* ---- Shorts setting ---- */
      document.querySelector('[data-setting-shorts]')?.addEventListener('change', async (event) => {
        const input = event.currentTarget;
        const status = document.querySelector('[data-comments-status]');
        input.disabled = true;
        try {
          const current = await fetch('/api/feed/preferences').then((r) => r.json());
          const preferences = current.preferences || {};
          const response = await fetch('/api/feed/preferences', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              preferred_topics: preferences.preferred_topics || [],
              muted_topics: preferences.muted_topics || [],
              prioritize_subscriptions: preferences.prioritize_subscriptions !== false,
              exclude_shorts: !input.checked,
              discovery_level: preferences.discovery_level ?? 0.35,
              show_comments: !!preferences.show_comments
            })
          });
          if (!response.ok) throw new Error('save failed');
          if (status) status.textContent = input.checked ? 'Shorts are shown in your feed.' : 'Shorts are hidden.';
          showToast(input.checked ? 'Shorts enabled' : 'Shorts hidden');
        } catch (error) {
          input.checked = !input.checked;
          if (status) status.textContent = 'Could not save this setting.';
        } finally {
          input.disabled = false;
        }
      });

      /* ---- estimated dislike counts (Return YouTube Dislike) ---- */
      document.querySelector('[data-setting-ryd]')?.addEventListener('change', async (event) => {
        const input = event.currentTarget;
        const status = document.querySelector('[data-comments-status]');
        input.disabled = true;
        try {
          const current = await fetch('/api/feed/preferences').then((r) => r.json());
          const preferences = current.preferences || {};
          const response = await fetch('/api/feed/preferences', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              preferred_topics: preferences.preferred_topics || [],
              muted_topics: preferences.muted_topics || [],
              prioritize_subscriptions: preferences.prioritize_subscriptions !== false,
              exclude_shorts: !!preferences.exclude_shorts,
              discovery_level: preferences.discovery_level ?? 0.35,
              show_comments: !!preferences.show_comments,
              ryd_enabled: input.checked
            })
          });
          if (!response.ok) throw new Error('save failed');
          if (status) {
            status.textContent = input.checked
              ? 'Dislike estimates will fill in over the next background sync.'
              : 'Dislike estimates are off. Counts already fetched stay cached.';
          }
          showToast(input.checked ? 'Dislike estimates enabled' : 'Dislike estimates disabled');
        } catch (error) {
          input.checked = !input.checked;
          if (status) status.textContent = 'Could not save this setting.';
        } finally {
          input.disabled = false;
        }
      });

      /* ---- playback sources and fallback control ---- */
      (() => {
        const list = document.querySelector('[data-playback-sources]');
        if (!list) return;
        const editor = document.querySelector('[data-instance-editor]');
        const chips = document.querySelector('[data-instance-chips]');
        const status = document.querySelector('[data-playback-status]');
        const toggles = [...document.querySelectorAll('[data-playback-toggle]')];
        let preferences = null;

        const GRIP = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 5h.01M9 12h.01M9 19h.01M15 5h.01M15 12h.01M15 19h.01"/></svg>';
        let currentSources = [];

        function healthLabel(source) {
          const health = source.health || {};
          if (source.in_cooldown) return `paused, retrying in ${health.cooldown_seconds}s`;
          if (health.status === 'healthy') return health.last_ok === null ? 'ready' : `worked ${health.last_ok}s ago`;
          if (health.failures) return `${health.failures} recent failure${health.failures === 1 ? '' : 's'}`;
          return 'not tried yet';
        }

        function setEnabled(source, enabled) {
          const disabled = new Set(preferences.disabled_sources || []);
          if (enabled) disabled.delete(source.id); else disabled.add(source.id);
          save({ disabled_sources: [...disabled] });
        }

        function persistOrder() {
          save({ source_order: currentSources.map((source) => source.id) }, { quiet: true });
        }

        function moveSource(from, to) {
          if (to < 0 || to >= currentSources.length) return;
          const [moved] = currentSources.splice(from, 1);
          currentSources.splice(to, 0, moved);
          renderSources(currentSources);
          persistOrder();
        }

        function renderSources(sources) {
          currentSources = sources;
          list.replaceChildren();
          if (!sources.length) {
            const empty = document.createElement('p');
            empty.className = 'dialog-note';
            empty.textContent = 'No sources enabled — nothing can play. Turn at least one on below.';
            list.appendChild(empty);
            return;
          }
          sources.forEach((source, position) => {
            const health = source.health || {};
            const row = document.createElement('div');
            const state = source.in_cooldown ? 'cooling' : health.status === 'healthy' ? 'healthy' : health.failures ? 'failing' : '';
            row.className = 'source-row ' + state + (source.enabled === false ? ' is-off' : '');
            row.draggable = true;
            row.dataset.sourceId = source.id;
            row.dataset.position = String(position);

            const grip = document.createElement('button');
            grip.type = 'button';
            grip.className = 'grip';
            grip.innerHTML = GRIP;
            grip.setAttribute('aria-label', `Reorder ${source.label}. Use arrow up and down.`);
            grip.addEventListener('keydown', (event) => {
              if (event.key === 'ArrowUp') { event.preventDefault(); moveSource(position, position - 1); }
              if (event.key === 'ArrowDown') { event.preventDefault(); moveSource(position, position + 1); }
            });

            const dot = document.createElement('span');
            dot.className = 'dot';

            const copy = document.createElement('span');
            copy.className = 'source-copy';
            const name = document.createElement('strong');
            name.textContent = source.label;
            const detail = document.createElement('small');
            detail.textContent = source.enabled === false ? 'Off · ' + (source.hint || '') : `#${position + 1} · ${healthLabel(source)}`;
            copy.append(name, detail);

            const move = document.createElement('span');
            move.className = 'source-move';
            [['\\u25b2', -1], ['\\u25bc', 1]].forEach(([glyph, delta]) => {
              const button = document.createElement('button');
              button.type = 'button';
              button.textContent = glyph;
              button.setAttribute('aria-label', `Move ${source.label} ${delta < 0 ? 'up' : 'down'}`);
              button.disabled = delta < 0 ? position === 0 : position === sources.length - 1;
              button.addEventListener('click', () => moveSource(position, position + delta));
              move.appendChild(button);
            });

            const toggle = document.createElement('input');
            toggle.type = 'checkbox';
            toggle.checked = source.enabled !== false;
            toggle.setAttribute('aria-label', `Use ${source.label}`);
            toggle.addEventListener('change', () => setEnabled(source, toggle.checked));

            row.append(grip, dot, copy, move, toggle);
            list.appendChild(row);
          });
          wireDragging();
        }

        function wireDragging() {
          let dragged = null;
          list.querySelectorAll('.source-row').forEach((row) => {
            row.addEventListener('dragstart', (event) => {
              dragged = row;
              row.classList.add('dragging');
              event.dataTransfer.effectAllowed = 'move';
              event.dataTransfer.setData('text/plain', row.dataset.sourceId || '');
            });
            row.addEventListener('dragend', () => {
              row.classList.remove('dragging');
              list.querySelectorAll('.drop-target').forEach((item) => item.classList.remove('drop-target'));
              dragged = null;
            });
            row.addEventListener('dragover', (event) => {
              if (!dragged || dragged === row) return;
              event.preventDefault();
              event.dataTransfer.dropEffect = 'move';
              row.classList.add('drop-target');
            });
            row.addEventListener('dragleave', () => row.classList.remove('drop-target'));
            row.addEventListener('drop', (event) => {
              event.preventDefault();
              row.classList.remove('drop-target');
              if (!dragged || dragged === row) return;
              moveSource(Number(dragged.dataset.position), Number(row.dataset.position));
            });
          });
        }

        function renderChips() {
          if (!chips) return;
          chips.replaceChildren();
          (preferences.public_invidious_urls || []).forEach((url) => {
            const chip = document.createElement('button');
            chip.type = 'button';
            chip.setAttribute('aria-label', `Remove ${url}`);
            const text = document.createElement('span');
            text.textContent = url.replace(/^https?:\\/\\//, '');
            const cross = document.createElement('em');
            cross.textContent = '\\u00d7';
            chip.append(text, cross);
            chip.addEventListener('click', () => {
              save({ public_invidious_urls: (preferences.public_invidious_urls || []).filter((item) => item !== url) });
            });
            chips.appendChild(chip);
          });
        }

        function applyPreferences() {
          toggles.forEach((input) => { input.checked = !!preferences[input.dataset.playbackToggle]; });
          /* The instance editor is always available — that is how a source gets added. */
          if (editor) editor.hidden = false;
          renderChips();
        }

        function renderDiagnosis(diagnosis) {
          const box = document.querySelector('[data-playback-diagnosis]');
          if (!box) return;
          box.replaceChildren();
          box.hidden = false;
          const title = document.createElement('h4');
          title.textContent = diagnosis.headline || '';
          const body = document.createElement('p');
          body.textContent = diagnosis.explanation || '';
          box.append(title, body);
          if ((diagnosis.steps || []).length) {
            const chain = document.createElement('div');
            chain.className = 'chain';
            diagnosis.steps.forEach((hop) => {
              const pill = document.createElement('span');
              pill.className = 'hop ' + (hop.ok ? 'ok' : 'bad');
              pill.textContent = hop.name + (hop.detail ? ` (${hop.detail})` : '');
              chain.appendChild(pill);
            });
            box.appendChild(chain);
          }
          if ((diagnosis.remedies || []).length) {
            const list_ = document.createElement('ul');
            diagnosis.remedies.forEach((item) => {
              const li = document.createElement('li');
              li.textContent = item;
              list_.appendChild(li);
            });
            box.appendChild(list_);
          }
        }

        async function load() {
          try {
            const response = await fetch('/api/playback/sources');
            if (!response.ok) throw new Error('load failed');
            const data = await response.json();
            preferences = data.preferences;
            applyPreferences();
            renderSources(data.sources || []);
            if (status && data.local_backend_degraded) status.textContent = 'Local Invidious is being skipped right now.';
          } catch (error) {
            list.replaceChildren();
            const failed = document.createElement('p');
            failed.className = 'dialog-note';
            failed.textContent = 'Could not load playback sources.';
            list.appendChild(failed);
          }
        }

        async function save(patch, options = {}) {
          try {
            const response = await fetch('/api/playback/preferences', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify(patch)
            });
            if (!response.ok) throw new Error('save failed');
            preferences = (await response.json()).preferences;
            applyPreferences();
            /* Reordering already re-rendered optimistically; refetching would fight it. */
            if (!options.quiet) await load();
            showToast(options.quiet ? 'Playback order saved' : 'Playback settings saved');
          } catch (error) {
            showToast('Could not save playback settings');
          }
        }

        toggles.forEach((input) => {
          input.addEventListener('change', () => save({ [input.dataset.playbackToggle]: input.checked }));
        });

        const instanceInput = document.querySelector('[data-instance-input]');
        function addInstance() {
          const value = (instanceInput?.value || '').trim();
          if (!value) return;
          save({ public_invidious_urls: [...(preferences.public_invidious_urls || []), value] });
          instanceInput.value = '';
        }
        document.querySelector('[data-instance-add]')?.addEventListener('click', addInstance);
        instanceInput?.addEventListener('keydown', (event) => {
          if (event.key === 'Enter') { event.preventDefault(); addInstance(); }
        });

        document.querySelector('[data-playback-test]')?.addEventListener('click', async (event) => {
          const button = event.currentTarget;
          button.disabled = true;
          if (status) status.textContent = 'Testing every source\\u2026';
          try {
            const response = await fetch('/api/playback/sources/test', { method: 'POST' });
            if (!response.ok) throw new Error('test failed');
            const data = await response.json();
            const working = (data.results || []).filter((result) => result.ok).length;
            if (status) status.textContent = `${working} of ${(data.results || []).length} sources can play video.`;
            await load();
          } catch (error) {
            if (status) status.textContent = 'Test failed.';
          } finally {
            button.disabled = false;
          }
        });

        document.querySelector('[data-playback-diagnose]')?.addEventListener('click', async (event) => {
          const button = event.currentTarget;
          button.disabled = true;
          if (status) status.textContent = 'Checking the playback chain\\u2026';
          try {
            const response = await fetch('/api/playback/diagnose');
            if (!response.ok) throw new Error('diagnose failed');
            const data = await response.json();
            renderDiagnosis(data.diagnosis || {});
            if (status) status.textContent = data.diagnosis?.ok ? 'Local playback is healthy.' : '';
          } catch (error) {
            if (status) status.textContent = 'Could not run the check.';
          } finally {
            button.disabled = false;
          }
        });

        /* Settings panes */
        document.querySelectorAll('[data-settings-pane]').forEach((tab) => {
          tab.addEventListener('click', () => {
            const key = tab.dataset.settingsPane;
            document.querySelectorAll('[data-settings-pane]').forEach((item) => {
              const active = item === tab;
              item.classList.toggle('active', active);
              item.setAttribute('aria-selected', active ? 'true' : 'false');
            });
            document.querySelectorAll('[data-settings-pane-body]').forEach((pane) => {
              pane.classList.toggle('active', pane.dataset.settingsPaneBody === key);
            });
            if (key === 'playback') load();
          });
        });

        /* Refresh the health readout whenever the settings dialog is opened. */
        document.addEventListener('click', (event) => {
          if (event.target.closest('[data-open-settings-dialog]')) window.setTimeout(load, 60);
        });
        load();
      })();

      /* ---- sidebar subscriptions refresh ---- */
      async function refreshSubscriptionNav() {
        const sidebar = document.querySelector('.desktop-sidebar');
        if (!sidebar) return;
        try {
          const response = await fetch('/api/sidebar/subscriptions', { headers: { 'Accept': 'text/html' } });
          if (!response.ok) return;
          const markup = (await response.text()).trim();
          const current = sidebar.querySelector('[data-subscription-nav]');
          if (!markup) { current?.remove(); return; }
          const parsed = document.createRange().createContextualFragment(markup).firstElementChild;
          if (!parsed) return;
          if (current) current.replaceWith(parsed);
          else sidebar.appendChild(parsed);
        } catch (error) {
          /* leave the existing list in place */
        }
      }

      /* ---- comments (opt-in via settings) ---- */
      function renderComment(item) {
        const row = document.createElement('article');
        row.className = 'comment';

        const avatar = document.createElement('span');
        avatar.className = item.avatar_url ? 'avatar has-image' : 'avatar';
        const initials = document.createElement('span');
        initials.className = 'avatar-initials';
        initials.textContent = (item.author || '?').replace(/^@/, '').slice(0, 2).toUpperCase();
        avatar.appendChild(initials);
        if (item.avatar_url) {
          const img = document.createElement('img');
          img.src = item.avatar_url;
          img.loading = 'lazy';
          img.alt = '';
          img.addEventListener('error', () => { avatar.classList.remove('has-image'); img.remove(); });
          avatar.appendChild(img);
        }

        const main = document.createElement('div');
        const head = document.createElement('div');
        head.className = 'comment-head';
        const author = document.createElement('span');
        author.className = item.is_owner ? 'comment-author owner' : 'comment-author';
        author.textContent = item.author || 'Unknown';
        head.appendChild(author);
        if (item.published_text) {
          const time = document.createElement('span');
          time.className = 'comment-time';
          time.textContent = item.published_text;
          head.appendChild(time);
        }
        if (item.is_pinned) {
          const pinned = document.createElement('span');
          pinned.className = 'comment-flag';
          pinned.textContent = 'Pinned';
          head.appendChild(pinned);
        }

        const body = document.createElement('p');
        body.className = 'comment-body';
        /* Built as nodes rather than innerHTML so comment text can never become markup.
           Timestamps become buttons that seek the player, the way they do on YouTube. */
        /* One pass over both patterns: a reply usually opens with an @mention and may
           also carry a timestamp, and splitting twice would lose whichever ran second. */
        (item.content || '').split(/((?:\\d{1,2}:)?\\d{1,2}:\\d{2}|@[A-Za-z0-9._-]{2,30})/).forEach((chunk) => {
          if (!chunk) return;
          if (/^(?:\\d{1,2}:)?\\d{1,2}:\\d{2}$/.test(chunk)) {
            const link = document.createElement('button');
            link.type = 'button';
            link.className = 'ts-link';
            link.dataset.seek = chunk;
            link.textContent = chunk;
            body.appendChild(link);
          } else if (chunk.startsWith('@')) {
            const mention = document.createElement('span');
            mention.className = 'comment-mention';
            mention.textContent = chunk;
            body.appendChild(mention);
          } else {
            body.appendChild(document.createTextNode(chunk));
          }
        });

        if (item.is_owner) row.classList.add('is-creator');
        if (item.reply_depth) row.classList.add(`reply-depth-${Math.min(item.reply_depth, 3)}`);

        const meta = document.createElement('div');
        meta.className = 'comment-meta';
        if (item.creator_heart) {
          /* Beside the vote buttons, where YouTube puts it. */
          const heart = document.createElement('span');
          heart.className = 'comment-heart';
          heart.title = `${item.creator_heart_name || 'The creator'} hearted this`;
          const face = document.createElement('img');
          face.src = item.creator_heart;
          face.alt = '';
          face.loading = 'lazy';
          face.addEventListener('error', () => face.remove());
          const pip = document.createElement('i');
          pip.textContent = '\u2764';
          heart.append(face, pip);
          meta.appendChild(heart);
        }

        /* Voting here is local only: Invidious reads comments, it cannot cast a vote on
           YouTube. The click is recorded as a small signal about the video instead. */
        const reactions = document.createElement('span');
        reactions.className = 'comment-reactions';
        const like = document.createElement('button');
        like.type = 'button';
        like.className = 'comment-vote';
        like.dataset.commentVote = 'like';
        like.dataset.commentId = item.comment_id || '';
        like.setAttribute('aria-pressed', item.reaction === 'like' ? 'true' : 'false');
        if (item.reaction === 'like') like.classList.add('is-selected');
        like.setAttribute('aria-label', 'Like this comment');
        like.innerHTML = LIKE_ICON;
        /* A zero is not worth the width. Leaving it in padded every uncounted comment
           and pushed the two thumbs apart for no information. */
        if (item.like_count) {
          const likeCount = document.createElement('span');
          likeCount.className = 'comment-vote-count';
          likeCount.textContent = formatCount(item.like_count);
          like.appendChild(likeCount);
        }

        const dislike = document.createElement('button');
        dislike.type = 'button';
        dislike.className = 'comment-vote';
        dislike.dataset.commentVote = 'dislike';
        dislike.dataset.commentId = item.comment_id || '';
        dislike.setAttribute('aria-pressed', item.reaction === 'dislike' ? 'true' : 'false');
        if (item.reaction === 'dislike') dislike.classList.add('is-selected');
        dislike.setAttribute('aria-label', 'Dislike this comment');
        dislike.innerHTML = DISLIKE_ICON;

        reactions.append(like, dislike);
        meta.appendChild(reactions);

        main.append(head, body, meta);

        if (item.reply_count && item.reply_continuation) {
          const label = `${formatCount(item.reply_count)} ${item.reply_count === 1 ? 'reply' : 'replies'}`;
          const toggle = document.createElement('button');
          toggle.type = 'button';
          toggle.className = 'reply-toggle';
          toggle.innerHTML = CHEVRON_ICON;
          const text = document.createElement('span');
          text.textContent = label;
          toggle.appendChild(text);
          toggle.setAttribute('aria-expanded', 'false');

          const thread = document.createElement('div');
          thread.className = 'comment-replies';
          thread.hidden = true;

          let loaded = false;
          toggle.addEventListener('click', async () => {
            const open = thread.hidden;
            toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
            toggle.classList.toggle('is-open', open);
            thread.hidden = !open;
            text.textContent = open ? 'Hide replies' : label;
            if (!open || loaded) return;
            loaded = true;
            thread.replaceChildren();
            const loading = document.createElement('p');
            loading.className = 'comments-state';
            loading.textContent = 'Loading replies\\u2026';
            thread.appendChild(loading);
            try {
              const videoId = document.querySelector('[data-comments-for]')?.dataset.commentsFor || '';
              const url = `/api/comments/${encodeURIComponent(videoId)}?continuation=${encodeURIComponent(item.reply_continuation)}`;
              const response = await fetch(url);
              if (!response.ok) throw new Error(String(response.status));
              const data = await response.json();
              thread.replaceChildren();
              const replies = data.items || [];
              assignReplyDepth(replies);
              if (!replies.length) {
                const empty = document.createElement('p');
                empty.className = 'comments-state';
                empty.textContent = 'No replies to show.';
                thread.appendChild(empty);
                return;
              }
              replies.forEach((reply) => thread.appendChild(renderComment(reply)));
              resolveCommentAvatars(thread);
            } catch (error) {
              loaded = false;
              thread.replaceChildren();
              const failed = document.createElement('p');
              failed.className = 'comments-state';
              failed.textContent = 'Replies could not be loaded.';
              thread.appendChild(failed);
            }
          });

          main.append(toggle, thread);
        } else if (item.reply_count) {
          const replies = document.createElement('span');
          replies.textContent = `${item.reply_count} ${item.reply_count === 1 ? 'reply' : 'replies'}`;
          meta.appendChild(replies);
        }

        row.append(avatar, main);
        row.dataset.authorId = item.author_id || '';
        return row;
      }

      /* Invidious's comments endpoint stopped returning authorThumbnails (empty for
         every comment), so rows render as initials first. Resolve the authors on
         screen against the batch endpoint and swap the pictures in; misses stay as
         initials. Fires once per rendered batch, not per row. */
      async function resolveCommentAvatars(root) {
        const rows = [...root.querySelectorAll('.comment[data-author-id]')].filter(
          (row) => row.dataset.authorId && !row.querySelector('.avatar img'));
        if (!rows.length) return;
        const ids = [...new Set(rows.map((row) => row.dataset.authorId))].slice(0, 25);
        let data = null;
        try {
          const response = await fetch('/api/comments/avatars', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ author_ids: ids }),
          });
          if (!response.ok) return;
          data = await response.json();
        } catch (error) {
          return;
        }
        const avatars = (data && data.avatars) || {};
        rows.forEach((row) => {
          const url = avatars[row.dataset.authorId];
          if (!url) return;
          const avatar = row.querySelector('.avatar');
          if (!avatar || avatar.querySelector('img')) return;
          const img = document.createElement('img');
          img.src = url;
          img.loading = 'lazy';
          img.alt = '';
          img.addEventListener('error', () => { avatar.classList.remove('has-image'); img.remove(); });
          avatar.classList.add('has-image');
          avatar.appendChild(img);
        });
      }

      /* Reply threads arrive flat: every reply sits at the same level even when it
         answers another reply (visible as a leading @mention, the way YouTube shows
         it). Rebuild the visual chain from mentions: a reply opening with @someone
         who wrote earlier in the thread hangs one level deeper, capped so a long
         back-and-forth cannot march off the page. Top-level comments never go
         through this, so only replies can nest. */
      const REPLY_DEPTH_CAP = 3;
      function assignReplyDepth(items) {
        const depthByAuthor = new Map();
        items.forEach((item) => {
          const handle = String(item.author || '').replace(/^@/, '').toLowerCase();
          const target = String(item.content || '').match(/^@([A-Za-z0-9._-]{2,30})\b/);
          let depth = 0;
          if (target) {
            const parent = depthByAuthor.get(target[1].toLowerCase());
            if (parent !== undefined) depth = Math.min(parent + 1, REPLY_DEPTH_CAP);
          }
          item.reply_depth = depth;
          depthByAuthor.set(handle, depth);
        });
      }

      function formatCount(value) {
        const n = Number(value) || 0;
        if (n >= 1000000) return (n / 1000000).toFixed(n >= 10000000 ? 0 : 1).replace(/\\.0$/, '') + 'M';
        if (n >= 1000) return (n / 1000).toFixed(n >= 10000 ? 0 : 1).replace(/\\.0$/, '') + 'K';
        return String(n);
      }

      /* Comments paginate as you scroll. Invidious returns a continuation token per page;
         without following it the page showed only the first ~20 of several thousand. */
      let commentCursor = '';
      let commentsBusy = false;
      let commentsDone = false;

      async function loadComments(more = false) {
        const section = document.querySelector('[data-comments-for]');
        if (!section || commentsBusy || (more && (commentsDone || !commentCursor))) return;
        commentsBusy = true;
        const videoId = section.dataset.commentsFor;
        const body = section.querySelector('[data-comments-body]');
        const counter = section.querySelector('[data-comments-count]');
        try {
          const query = more ? `?continuation=${encodeURIComponent(commentCursor)}` : '';
          const response = await fetch(`/api/comments/${encodeURIComponent(videoId)}${query}`);
          if (!response.ok) throw new Error(String(response.status));
          const data = await response.json();
          const items = data.items || [];
          commentCursor = data.continuation || '';
          if (!commentCursor) commentsDone = true;
          if (!more) body.replaceChildren();
          if (counter && data.comment_count != null) counter.textContent = formatCount(data.comment_count);
          if (!items.length) {
            if (more) { commentsDone = true; return; }
            const empty = document.createElement('p');
            empty.className = 'comments-state';
            empty.textContent = 'No comments on this video.';
            body.appendChild(empty);
            return;
          }
          items.forEach((item) => body.appendChild(renderComment(item)));
          resolveCommentAvatars(body);
        } catch (error) {
          if (more) { commentsDone = true; return; }
          body.replaceChildren();
          const failed = document.createElement('p');
          failed.className = 'comments-state';
          failed.textContent = 'Comments could not be loaded right now.';
          body.appendChild(failed);
        } finally {
          commentsBusy = false;
        }
      }
      loadComments();

      (() => {
        const section = document.querySelector('[data-comments-for]');
        if (!section || !('IntersectionObserver' in window)) return;
        const sentinel = document.createElement('div');
        sentinel.className = 'feed-sentinel';
        section.appendChild(sentinel);
        /* Fetches before the sentinel is on screen, so the next page is usually already
           in place by the time you reach the bottom. */
        new IntersectionObserver((entries) => {
          if (entries.some((entry) => entry.isIntersecting)) loadComments(true);
        }, { rootMargin: '600px 0px' }).observe(sentinel);
      })();

      /* ---- comments setting ---- */
      document.querySelector('[data-setting-comments]')?.addEventListener('change', async (event) => {
        const input = event.currentTarget;
        const status = document.querySelector('[data-comments-status]');
        input.disabled = true;
        if (status) status.textContent = 'Saving…';
        try {
          const current = await fetch('/api/feed/preferences').then((r) => r.json());
          const preferences = current.preferences || {};
          const response = await fetch('/api/feed/preferences', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              preferred_topics: preferences.preferred_topics || [],
              muted_topics: preferences.muted_topics || [],
              prioritize_subscriptions: preferences.prioritize_subscriptions !== false,
              exclude_shorts: !!preferences.exclude_shorts,
              discovery_level: preferences.discovery_level ?? 0.35,
              show_comments: input.checked
            })
          });
          if (!response.ok) throw new Error('save failed');
          if (status) status.textContent = input.checked
            ? 'Comments will appear on watch pages.'
            : 'Comments are hidden.';
          showToast(input.checked ? 'Comments enabled' : 'Comments disabled');
        } catch (error) {
          input.checked = !input.checked;
          if (status) status.textContent = 'Could not save this setting.';
        } finally {
          input.disabled = false;
        }
      });

      /* ---- sidebar ---- */
      (() => {
        const body = document.body;
        /* Watch and Shorts start with the rail out of the way — the video is the point
           of the page. Everywhere else the choice persists between visits. */
        const immersive = body.classList.contains('immersive-nav');
        if (immersive) {
          body.classList.add('nav-collapsed', 'nav-overlay');
        } else if (localStorage.getItem('openfeed-nav') === 'collapsed') {
          body.classList.add('nav-collapsed');
        }
        /* Only animate after the first paint so the initial state does not slide in. */
        window.requestAnimationFrame(() => body.classList.add('nav-ready'));

        const scrim = document.querySelector('[data-nav-scrim]');
        function setCollapsed(collapsed) {
          body.classList.toggle('nav-collapsed', collapsed);
          if (!immersive) localStorage.setItem('openfeed-nav', collapsed ? 'collapsed' : 'open');
        }

        document.querySelector('[data-nav-toggle]')?.addEventListener('click', () => {
          setCollapsed(!body.classList.contains('nav-collapsed'));
        });
        scrim?.addEventListener('click', () => setCollapsed(true));
        document.addEventListener('keydown', (event) => {
          if (event.key === 'Escape' && immersive && !body.classList.contains('nav-collapsed')) setCollapsed(true);
        });
      })();

      /* ---- theater mode ---- */
      (() => {
        const toggle = document.querySelector('[data-theater-toggle]');
        if (!toggle) return;
        const body = document.body;
        function apply(on) {
          body.classList.toggle('theater', on);
          toggle.setAttribute('aria-pressed', on ? 'true' : 'false');
          toggle.classList.toggle('is-selected', on);
          localStorage.setItem('openfeed-theater', on ? 'on' : 'off');
        }
        apply(localStorage.getItem('openfeed-theater') === 'on');
        toggle.addEventListener('click', () => apply(!body.classList.contains('theater')));
        document.addEventListener('keydown', (event) => {
          if (event.key !== 't' && event.key !== 'T') return;
          const tag = (event.target.tagName || '').toLowerCase();
          if (tag === 'input' || tag === 'textarea' || event.target.isContentEditable) return;
          if (event.metaKey || event.ctrlKey || event.altKey) return;
          apply(!body.classList.contains('theater'));
        });
      })();

      /* ---- download ---- */
      (() => {
        const dialog = document.querySelector('[data-download-dialog]');

        function formatSize(bytes) {
          if (!bytes) return '';
          const units = ['B', 'KB', 'MB', 'GB'];
          let value = bytes;
          let unit = 0;
          while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit += 1; }
          return `${value >= 10 || unit === 0 ? Math.round(value) : value.toFixed(1)} ${units[unit]}`;
        }

        function startDownload(videoId, itag, filename, quality) {
          /* A plain anchor click keeps the browser's own download UI and resume support. */
          const link = document.createElement('a');
          const query = itag ? `?itag=${encodeURIComponent(itag)}` : '';
          link.href = `/api/download/${encodeURIComponent(videoId)}${query}`;
          link.download = filename || '';
          document.body.appendChild(link);
          link.click();
          link.remove();
          showToast(quality ? `Downloading ${quality}` : 'Download started');
        }

        function formatRate(bps) {
          return bps ? `${formatSize(bps)}/s` : '';
        }

        function formatEta(seconds) {
          if (!seconds || seconds < 0) return '';
          const m = Math.floor(seconds / 60);
          const s = Math.round(seconds % 60);
          return m ? `${m}m ${s}s left` : `${s}s left`;
        }

        function showOptions(show) {
          dialog.querySelector('[data-download-options]').hidden = !show;
          dialog.querySelector('[data-download-destination]').hidden = !show;
          dialog.querySelector('[data-download-progress]').hidden = show;
        }

        function chosenDestination() {
          return dialog.querySelector('[data-destination].active')?.dataset.destination || 'local';
        }

        function renderOptions(videoId, data) {
          const list = dialog.querySelector('[data-download-options]');
          const note = dialog.querySelector('[data-download-note]');
          const subtitle = dialog.querySelector('[data-download-subtitle]');
          showOptions(true);
          list.textContent = '';
          if (!data.ok || !(data.formats || []).length) {
            const empty = document.createElement('p');
            empty.className = 'dialog-note';
            empty.textContent = data.detail || 'No downloadable stream is available for this video.';
            list.appendChild(empty);
            note.hidden = true;
            return;
          }
          subtitle.textContent = data.title || 'Choose a quality.';
          data.formats.forEach((format) => {
            const option = document.createElement('button');
            option.type = 'button';
            option.className = 'download-option';
            option.dataset.downloadMode = format.mode || 'direct';
            option.dataset.downloadItag = format.itag || '';
            option.dataset.downloadHeight = String(format.height || 0);
            option.dataset.downloadContainer = format.container || 'mp4';
            option.dataset.downloadMerge = format.needs_merge ? '1' : '';
            option.dataset.downloadFilename = format.filename || '';
            option.dataset.downloadQuality = format.quality || '';
            option.innerHTML = '<span class="menu-icon"></span><span></span><span class="download-size"></span>';
            option.querySelector('.menu-icon').innerHTML = ICON_DOWNLOAD;
            const copy = option.children[1];
            const title = document.createElement('strong');
            title.textContent = format.quality || 'Unknown quality';
            const detail = document.createElement('small');
            detail.textContent = format.needs_merge
              ? `${(format.container || 'mp4').toUpperCase()} \\u00b7 video and audio muxed on the server`
              : `${(format.container || 'mp4').toUpperCase()} \\u00b7 ready to stream straight through`;
            copy.append(title, detail);
            /* A merged size is derived from the tracks yt-dlp is expected to pick, so it
               is close but not exact — the tilde says so rather than implying precision. */
            const size = formatSize(format.size_bytes);
            option.querySelector('.download-size').textContent = size && format.size_estimated ? `~${size}` : size;
            list.appendChild(option);
          });
          if (!data.ytdlp) {
            note.textContent = 'yt-dlp is not available here, so only the renditions YouTube publishes as a single file are listed.';
            note.hidden = false;
          } else if (!data.ffmpeg) {
            note.textContent = 'ffmpeg is not installed, so resolutions that need video and audio merged are unavailable.';
            note.hidden = false;
          } else {
            note.hidden = true;
          }
        }

        /* --- merged downloads run as a job, because muxing takes real time --- */
        let pollTimer = null;
        let activeJob = '';

        function stopPolling() {
          if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
        }

        function renderJob(job) {
          const label = dialog.querySelector('[data-download-progress-label]');
          const bar = dialog.querySelector('.download-bar');
          const fill = dialog.querySelector('[data-download-bar]');
          const stats = dialog.querySelector('[data-download-stats]');
          const wording = { queued: 'Queued\\u2026', downloading: `Downloading ${job.quality || ''}`.trim(), merging: 'Merging video and audio\\u2026', done: 'Ready', error: 'Failed', cancelled: 'Cancelled' };
          label.textContent = wording[job.status] || job.status;
          /* Size is unknown until yt-dlp reports it, and unknowable during the merge —
             a sweeping bar is honest there, a fake percentage is not. */
          const indeterminate = job.status === 'merging' || (!job.total_bytes && job.status !== 'done');
          bar.classList.toggle('is-indeterminate', indeterminate);
          fill.style.width = indeterminate ? '' : `${job.percent || 0}%`;
          stats.textContent = job.status === 'error'
            ? job.error
            : [job.total_bytes ? `${formatSize(job.downloaded_bytes)} of ${formatSize(job.total_bytes)}` : '', formatRate(job.speed_bps), formatEta(job.eta_seconds)].filter(Boolean).join(' \\u00b7 ');
        }

        async function pollJob(jobId) {
          if (activeJob !== jobId) return;
          let job;
          try {
            const data = await fetch(`/api/download/jobs/${encodeURIComponent(jobId)}`).then((r) => r.json());
            job = data.job;
          } catch (error) {
            showToast('Lost track of the download');
            return;
          }
          if (!job || activeJob !== jobId) return;
          renderJob(job);
          if (job.status === 'done') {
            activeJob = '';
            if (job.destination === 'server') {
              showToast(`${job.quality || 'Download'} saved to your Library`, { label: 'Open', onClick: () => { window.location.href = '/downloads'; } });
              dialog.close();
              return;
            }
            const link = document.createElement('a');
            link.href = `/api/download/jobs/${encodeURIComponent(jobId)}/file`;
            link.download = job.filename || '';
            document.body.appendChild(link);
            link.click();
            link.remove();
            showToast(`${job.quality || 'Download'} ready`);
            dialog.close();
            return;
          }
          if (job.status === 'error' || job.status === 'cancelled') {
            activeJob = '';
            if (job.status === 'error') showToast('Download failed');
            return;
          }
          pollTimer = setTimeout(() => pollJob(jobId), 900);
        }

        async function startJob(videoId, height, container, needsMerge, title, destination) {
          showOptions(false);
          dialog.querySelector('[data-download-progress-label]').textContent = 'Starting\\u2026';
          dialog.querySelector('.download-bar').classList.add('is-indeterminate');
          dialog.querySelector('[data-download-stats]').textContent = '';
          try {
            const response = await fetch(`/api/download/${encodeURIComponent(videoId)}/jobs`, {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ height: Number(height) || 0, container, needs_merge: !!needsMerge, title: title || '', destination: destination || 'local' })
            });
            const data = await response.json();
            if (!response.ok) throw new Error(data.detail || 'could not start');
            activeJob = data.job.id;
            renderJob(data.job);
            pollJob(activeJob);
          } catch (error) {
            showOptions(true);
            showToast(String(error.message || 'Could not start the download'));
          }
        }

        document.addEventListener('click', (event) => {
          const pick = event.target.closest('[data-destination]');
          if (!pick || !dialog || !dialog.contains(pick)) return;
          dialog.querySelectorAll('[data-destination]').forEach((option) => {
            const on = option === pick;
            option.classList.toggle('active', on);
            option.setAttribute('aria-checked', on ? 'true' : 'false');
          });
        });

        document.addEventListener('click', async (event) => {
          const cancel = event.target.closest('[data-download-cancel]');
          if (cancel && dialog) {
            event.preventDefault();
            const jobId = activeJob;
            activeJob = '';
            stopPolling();
            showOptions(true);
            if (jobId) fetch(`/api/download/jobs/${encodeURIComponent(jobId)}`, { method: 'DELETE' }).catch(() => {});
            return;
          }

          const option = event.target.closest('[data-download-mode]');
          if (option && dialog) {
            event.preventDefault();
            const videoId = dialog.dataset.videoId || '';
            const destination = chosenDestination();
            /* The passthrough streams straight to the browser and never lands on disk,
               so keeping it on the server has to go through the job runner instead. */
            if (option.dataset.downloadMode === 'direct' && destination !== 'server') {
              /* Invidious already has this as one file — stream it straight through
                 rather than spending a merge on work that is already done. */
              startDownload(videoId, option.dataset.downloadItag, option.dataset.downloadFilename, option.dataset.downloadQuality);
              dialog.close();
              return;
            }
            startJob(videoId, option.dataset.downloadHeight, option.dataset.downloadContainer, option.dataset.downloadMerge, dialog.dataset.videoTitle || '', destination);
            return;
          }

          const button = event.target.closest('[data-download-video]');
          if (!button) return;
          event.preventDefault();
          const videoId = button.dataset.downloadVideo;
          if (!videoId) return;

          /* Outside the watch page there is no dialog in the DOM, so fall back to the
             single best rendition rather than doing nothing. */
          if (!dialog) {
            button.disabled = true;
            showToast('Preparing download\\u2026');
            try {
              const info = await fetch(`/api/download/${encodeURIComponent(videoId)}/info`).then((r) => r.json());
              if (!info.ok) throw new Error(info.detail || 'unavailable');
              startDownload(videoId, '', info.filename, info.quality);
            } catch (error) {
              showToast('No downloadable stream for this video');
            } finally {
              button.disabled = false;
            }
            return;
          }

          dialog.dataset.videoId = videoId;
          dialog.dataset.videoTitle = button.dataset.videoTitle || '';
          stopPolling();
          activeJob = '';
          showOptions(true);
          dialog.querySelector('[data-download-options]').innerHTML = '<p class="dialog-note">Checking what is available\\u2026</p>';
          dialog.querySelector('[data-download-note]').hidden = true;
          dialog.showModal();
          try {
            const data = await fetch(`/api/download/${encodeURIComponent(videoId)}/formats`).then((r) => r.json());
            if (dialog.dataset.videoId === videoId) renderOptions(videoId, data);
          } catch (error) {
            renderOptions(videoId, { ok: false, detail: 'Could not reach the playback backend.' });
          }
        });
      })();

      /* ---- dislike estimate arriving ---- */
      (() => {
        const slot = document.querySelector('[data-dislike-pending]');
        if (!slot) return;
        const videoId = slot.dataset.dislikePending;
        /* The batch is paced to look nothing like a session, so it takes tens of seconds.
           This polls our own database, never RYD, and gives up rather than running for as
           long as the tab happens to stay open. */
        let attempts = 0;
        const timer = window.setInterval(async () => {
          attempts += 1;
          if (attempts > 20) { window.clearInterval(timer); return; }
          try {
            const data = await fetch(`/api/videos/${encodeURIComponent(videoId)}/dislikes`).then((r) => r.json());
            if (!data.display && !data.checked) return;
            window.clearInterval(timer);
            if (!data.display) {
              slot.title = 'No estimate is available for this video.';
              return;
            }
            slot.classList.remove('muted');
            slot.classList.toggle('is-estimate', !!data.estimated);
            slot.textContent = data.display;
            slot.title = data.estimated
              ? 'Estimated by Return YouTube Dislike \\u2014 not an official count'
              : 'Dislike count as published before YouTube removed them in 2021';
          } catch (error) {
            window.clearInterval(timer);
          }
        }, 3000);
      })();

      /* ---- channel feed: keep loading as you scroll ---- */
      (() => {
        const grid = document.querySelector('[data-channel-grid]');
        const sentinel = document.querySelector('[data-channel-sentinel]');
        if (!grid || !sentinel || !('IntersectionObserver' in window)) return;
        const spinner = document.querySelector('[data-channel-loading]');
        let offset = Number(grid.dataset.nextOffset || 0);
        let loading = false;
        let done = false;

        async function loadMore() {
          if (loading || done) return;
          loading = true;
          if (spinner) spinner.hidden = false;
          try {
            const url = `/api/channels/${encodeURIComponent(grid.dataset.channelId)}/videos/html`
              + `?offset=${offset}&sort=${encodeURIComponent(grid.dataset.sort || 'latest')}`
              + `&kind=${encodeURIComponent(grid.dataset.kind || 'videos')}`
              + `&q=${encodeURIComponent(grid.dataset.q || '')}`;
            const data = await fetch(url).then((r) => r.json());
            if (data.html) {
              grid.insertAdjacentHTML('beforeend', data.html);
              grid.dataset.nextOffset = String(data.next_offset ?? offset);
            } else {
              /* An empty page with more behind it (a sparse Shorts window) still
                 advances: the offset moves but nothing renders. */
              grid.dataset.nextOffset = String(data.next_offset ?? offset);
            }
            offset = Number(grid.dataset.nextOffset || 0);
            /* Stop only when the server says the well is dry. An empty page alone
               is not the end (Shorts windows can scan past long-form videos). */
            if (!data.has_more) done = true;
          } catch (error) {
            done = true;
          } finally {
            loading = false;
            if (spinner) spinner.hidden = true;
          }
        }

        /* rootMargin starts the fetch before the sentinel is actually visible, so the
           next batch is usually already in place by the time the user reaches it. */
        new IntersectionObserver((entries) => {
          if (entries.some((entry) => entry.isIntersecting)) loadMore();
        }, { rootMargin: '800px 0px' }).observe(sentinel);
      })();

      /* ---- search results: keep loading YouTube pages as you scroll ---- */
      (() => {
        const list = document.querySelector('[data-search-list]');
        const sentinel = document.querySelector('[data-search-sentinel]');
        if (!list || !sentinel || !('IntersectionObserver' in window)) return;
        /* Page 1 already shows every library match, so with no YouTube results
           there is nothing further to load. */
        if (list.dataset.searchOk === '0') return;
        const spinner = document.querySelector('[data-search-loading]');
        let page = Number(list.dataset.nextPage || 2);
        let loading = false;
        let done = false;
        const seen = new Set(
          Array.from(list.querySelectorAll('[data-video-id]')).map((el) => el.dataset.videoId)
        );

        async function loadMore() {
          if (loading || done) return;
          loading = true;
          if (spinner) spinner.hidden = false;
          /* A sparse page (everything on it already shown) still advances, and
             chains straight into the next one so the scroll never stalls. */
          let added = 0;
          try {
            const data = await fetch(`/api/search/html?q=${encodeURIComponent(list.dataset.q || '')}&page=${page}`).then((r) => r.json());
            if (data.html) {
              const doc = new DOMParser().parseFromString(data.html, 'text/html');
              Array.from(doc.querySelectorAll('[data-video-id]'))
                .filter((el) => {
                  const id = el.dataset.videoId;
                  if (!id || seen.has(id)) return false;
                  seen.add(id);
                  return true;
                })
                .forEach((el) => { list.appendChild(el); added += 1; });
            }
            page = Number(data.next_page ?? (page + 1));
            list.dataset.nextPage = String(page);
            if (!data.has_more) done = true;
          } catch (error) {
            done = true;
          } finally {
            loading = false;
            if (spinner) spinner.hidden = true;
            if (!done && added === 0) loadMore();
          }
        }

        new IntersectionObserver((entries) => {
          if (entries.some((entry) => entry.isIntersecting)) loadMore();
        }, { rootMargin: '800px 0px' }).observe(sentinel);
      })();

      /* ---- takeout import ---- */
      (() => {
        const drop = document.querySelector('[data-import-drop]');
        if (!drop) return;
        const input = document.querySelector('[data-import-file]');
        const progress = document.querySelector('[data-import-progress]');
        const state = document.querySelector('[data-import-state]');
        const result = document.querySelector('[data-import-result]');

        function show(node, on) { if (node) node.hidden = !on; }

        function report(data, ok) {
          show(progress, false);
          show(result, true);
          result.classList.toggle('is-error', !ok);
          if (!ok) {
            result.innerHTML = '';
            const p = document.createElement('p');
            p.textContent = data;
            result.appendChild(p);
            return;
          }
          const s = data.summary || {};
          const top = (s.top_channels || []).slice(0, 5);
          result.innerHTML = '';
          const heading = document.createElement('strong');
          heading.textContent = 'Import complete';
          const counts = document.createElement('p');
          counts.textContent = `${(s.watched_videos || 0).toLocaleString()} videos across ${(s.watched_channels || 0).toLocaleString()} channels, and ${(s.subscriptions || 0).toLocaleString()} subscriptions.`;
          result.append(heading, counts);
          if (top.length) {
            const label = document.createElement('p');
            label.textContent = 'Most watched:';
            const list = document.createElement('ul');
            top.forEach((c) => {
              const li = document.createElement('li');
              li.textContent = `${c.title} — ${(c.watch_count || 0).toLocaleString()} videos`;
              list.appendChild(li);
            });
            result.append(label, list);
          }
          const note = document.createElement('p');
          note.textContent = 'Your feed will reflect this on the next refresh.';
          result.appendChild(note);
        }

        async function upload(file) {
          if (!file) return;
          if (!file.name.toLowerCase().endsWith('.zip')) {
            report('That is not a .zip archive. Upload the file Takeout gave you, unmodified.', false);
            return;
          }
          show(result, false);
          show(progress, true);
          /* Large archives take a while to send and a while to parse, so the copy says
             what is actually happening rather than sitting on one message. */
          if (state) state.textContent = `Uploading ${(file.size / (1024 * 1024)).toFixed(0)} MB\u2026`;
          const body = new FormData();
          body.append('archive', file);
          try {
            const response = await fetch('/api/takeout/import', { method: 'POST', body });
            const data = await response.json();
            if (!response.ok) { report(data.detail || 'That archive could not be imported.', false); return; }
            report(data, true);
          } catch (error) {
            report('The upload failed. If the archive is very large, try exporting only history and subscriptions.', false);
          }
        }

        drop.addEventListener('click', () => input?.click());
        drop.addEventListener('keydown', (event) => {
          if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); input?.click(); }
        });
        input?.addEventListener('change', () => upload(input.files?.[0]));
        ['dragenter', 'dragover'].forEach((name) => drop.addEventListener(name, (event) => {
          event.preventDefault(); drop.classList.add('is-over');
        }));
        ['dragleave', 'drop'].forEach((name) => drop.addEventListener(name, () => drop.classList.remove('is-over')));
        drop.addEventListener('drop', (event) => {
          event.preventDefault();
          upload(event.dataTransfer?.files?.[0]);
        });

        document.querySelector('[data-takeout-delete]')?.addEventListener('click', async (event) => {
          if (!window.confirm('Delete everything the import added? Your own watch history in OpenFeed is not affected.')) return;
          const button = event.currentTarget;
          button.disabled = true;
          try {
            const response = await fetch('/api/takeout', { method: 'DELETE' });
            if (!response.ok) throw new Error('delete failed');
            document.querySelector('[data-import-status]')?.remove();
            showToast('Imported data deleted');
          } catch (error) {
            button.disabled = false;
            showToast('Could not delete the imported data');
          }
        });
      })();

      /* ---- downloads library ---- */
      (() => {
        const grid = document.querySelector('[data-downloads-grid]');
        if (!grid) return;
        const search = document.querySelector('[data-downloads-search]');
        const summary = document.querySelector('[data-downloads-summary]');

        /* Filtering happens on the cards already rendered: the whole list is on the page,
           so a round trip per keystroke would be slower and no more accurate. */
        search?.addEventListener('input', () => {
          const needle = search.value.trim().toLowerCase();
          let shown = 0;
          grid.querySelectorAll('[data-video-card]').forEach((card) => {
            const hit = !needle || card.textContent.toLowerCase().includes(needle);
            card.hidden = !hit;
            if (hit) shown += 1;
          });
          if (summary) summary.textContent = needle ? `${shown} matching` : summary.dataset.original || summary.textContent;
        });
        if (summary) summary.dataset.original = summary.textContent;

        document.addEventListener('click', async (event) => {
          const remove = event.target.closest('[data-remove-download]');
          if (!remove) return;
          event.preventDefault();
          const videoId = remove.dataset.removeDownload;
          if (!window.confirm('Delete the downloaded file? The video stays in your feed.')) return;
          remove.disabled = true;
          try {
            const response = await fetch(`/api/downloads/${encodeURIComponent(videoId)}`, { method: 'DELETE' });
            if (!response.ok) throw new Error('delete failed');
            remove.closest('[data-video-card]')?.remove();
            showToast('Download deleted');
          } catch (error) {
            remove.disabled = false;
            showToast('Could not delete that download');
          }
        });
      })();

      /* ---- settings deep link ---- */
      (() => {
        const target = new URLSearchParams(window.location.search).get('settings');
        if (!target) return;
        const dialog = document.querySelector('#settings-dialog');
        if (!dialog) return;
        /* Fires after the pane wiring below has bound, so switching panes works. */
        window.setTimeout(() => {
          dialog.showModal();
          document.querySelector(`[data-settings-pane="${target}"]`)?.click();
          /* Drop the parameter so a reload does not reopen the dialog. */
          window.history.replaceState(null, '', window.location.pathname);
        }, 0);
      })();

      /* ---- sidebar: show more subscriptions ---- */
      document.addEventListener('click', (event) => {
        const button = event.target.closest('[data-subscription-more]');
        if (!button) return;
        const nav = button.closest('[data-subscription-nav]');
        const expanded = button.getAttribute('aria-expanded') === 'true';
        nav?.querySelectorAll('.nav-channel-extra').forEach((row) => { row.hidden = expanded; });
        button.setAttribute('aria-expanded', expanded ? 'false' : 'true');
        const label = button.querySelector('span');
        if (label) label.textContent = expanded ? button.dataset.moreLabel : button.dataset.lessLabel;
      });

      /* ---- sponsorblock settings ---- */
      document.querySelector('[data-setting-sponsorblock]')?.addEventListener('change', async (event) => {
        const input = event.currentTarget;
        input.disabled = true;
        try {
          const response = await fetch('/api/feed/preferences', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ sponsorblock_enabled: input.checked })
          });
          if (!response.ok) throw new Error('save failed');
          document.querySelector('[data-sponsorblock-categories]').hidden = !input.checked;
          showToast(input.checked ? 'SponsorBlock enabled' : 'SponsorBlock disabled');
        } catch (error) {
          input.checked = !input.checked;
          showToast('Could not save this setting');
        } finally {
          input.disabled = false;
        }
      });

      document.addEventListener('change', async (event) => {
        const select = event.target.closest('[data-sponsorblock-category]');
        if (!select) return;
        const actions = {};
        document.querySelectorAll('[data-sponsorblock-category]').forEach((node) => {
          actions[node.dataset.sponsorblockCategory] = node.value;
        });
        try {
          await fetch('/api/feed/preferences', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ sponsorblock_actions: actions })
          });
          showToast('SponsorBlock updated');
        } catch (error) {
          showToast('Could not save that change');
        }
      });

      /* ---- community posts: simulated poll voting ---- */
      document.addEventListener('click', async (event) => {
        const clear = event.target.closest('[data-poll-clear]');
        const choice = event.target.closest('[data-poll-vote]');
        if (!clear && !choice) return;
        event.preventDefault();
        const postId = clear ? clear.dataset.pollClear : choice.dataset.pollVote;
        const index = clear ? -1 : Number(choice.dataset.choice);
        const poll = document.querySelector(`[data-poll="${safeCss(postId)}"]`);
        /* Painted before the request returns: this is a local record, so there is nothing
           to be authoritative about and waiting would only make it feel slow. */
        poll?.querySelectorAll('[data-poll-vote]').forEach((node) => {
          const picked = !clear && Number(node.dataset.choice) === index;
          node.classList.toggle('is-picked', picked);
          node.setAttribute('aria-pressed', picked ? 'true' : 'false');
        });
        try {
          await fetch(`/api/posts/${encodeURIComponent(postId)}/vote`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ choice_index: clear ? null : index })
          });
          showToast(clear ? 'Answer cleared' : 'Saved here only \u2014 not sent to YouTube');
        } catch (error) {
          showToast('Could not save your answer');
        }
      });

      /* ---- show posts in feeds ---- */
      document.querySelector('[data-setting-posts]')?.addEventListener('change', async (event) => {
        const input = event.currentTarget;
        input.disabled = true;
        try {
          const response = await fetch('/api/feed/preferences', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ show_posts_in_feeds: input.checked })
          });
          if (!response.ok) throw new Error('save failed');
          showToast(input.checked ? 'Posts will appear in your feeds' : 'Posts hidden from feeds');
        } catch (error) {
          input.checked = !input.checked;
          showToast('Could not save this setting');
        } finally {
          input.disabled = false;
        }
      });

      /* ---- feed controls ---- */
      (() => {
        /* Measured panels load when the dialog opens, not on every page render. */
        const dialog = document.querySelector('#feed-dialog');
        if (!dialog) return;
        let loaded = false;
        const SIGNAL_LABELS = {
          semantic_affinity: 'Topic similarity', subscription_topic_affinity: 'Subscription topics',
          watch_time_channel: 'Channels you watch', watch_time_topic: 'Subjects you watch',
          takeout_channel: 'Imported history', takeout_already_watched: 'Already watched',
          preferred_topic: 'Followed topics', muted_topic: 'Muted topics',
          discovery_balance: 'Discovery balance', quick_abandonment: 'Videos you bounced off',
          partial_watch_penalty: 'Part-watched',
        };

        async function loadComposition() {
          if (loaded) return;
          loaded = true;
          try {
            const data = await fetch('/api/feed/composition').then((r) => r.json());
            const cov = data.coverage || {};
            const total = cov.videos || 0;
            const sources = cov.sources || {};
            const subs = Math.round(((sources.subscription || 0) / (total || 1)) * 100);
            const box = dialog.querySelector('[data-fc-composition]');
            if (box && total) {
              box.innerHTML = `<div class="fc-measure">
                <div class="fc-bar"><span class="fc-bar-fill" style="width:${subs}%"></span></div>
                <div class="fc-legend"><span><i class="fc-dot subs"></i>From channels you follow <b>${subs}%</b></span>
                <span><i class="fc-dot disc"></i>Discovered <b>${100 - subs}%</b></span></div>
                <p class="fc-caption">Counted from the ${total} videos ranked for you right now &mdash;
                ${cov.distinct_channels || 0} different creators, and no single one holds more than
                ${Math.round((cov.top_channel_share || 0) * 100)}% of the feed.</p></div>`;
            }
            const signals = dialog.querySelector('[data-fc-signals]');
            if (signals) {
              const chips = Object.entries(cov.signal_coverage || {})
                .filter(([name]) => SIGNAL_LABELS[name])
                .map(([name, share]) => `<span class="fc-signal"><b>${SIGNAL_LABELS[name]}</b><i>${Math.round(share * 100)}%</i></span>`);
              signals.innerHTML = chips.join('') || '<span class="fc-signal muted"><b>No signals yet</b></span>';
            }
            const channels = dialog.querySelector('[data-fc-channels]');
            if (channels) {
              channels.innerHTML = (data.channels || []).map((row) => {
                const cls = row.muted ? 'fc-channel is-muted' : 'fc-channel';
                return `<button class="${cls}" type="button" data-mute-channel="${row.key}" aria-pressed="${row.muted}">`
                  + `<span></span><small>${row.count}</small><i class="fc-channel-mark"></i></button>`;
              }).join('') || '<p class="fc-caption">Nothing to show yet.</p>';
              /* Titles set as text, never interpolated into markup. */
              [...channels.querySelectorAll('.fc-channel')].forEach((node, index) => {
                node.querySelector('span').textContent = (data.channels[index] || {}).title || '';
              });
            }
          } catch (error) {
            loaded = false;
          }
        }
        document.addEventListener('click', (event) => {
          if (event.target.closest('[data-open-feed-dialog]')) loadComposition();
        });

        const slider = document.querySelector('[data-tune-input]');
        const value = document.querySelector('[data-tune-value]');
        if (!slider) return;
        const paint = () => {
          slider.style.setProperty('--fill', `${slider.value}%`);
          if (value) value.textContent = `${slider.value}%`;
        };
        paint();
        slider.addEventListener('input', paint);
      })();

      document.addEventListener('click', async (event) => {
        const chip = event.target.closest('[data-mute-channel]');
        if (!chip) return;
        event.preventDefault();
        const muted = chip.classList.toggle('is-muted');
        chip.setAttribute('aria-pressed', muted ? 'true' : 'false');
        const keys = [...document.querySelectorAll('[data-mute-channel].is-muted')]
          .map((node) => node.dataset.muteChannel);
        try {
          await fetch('/api/feed/preferences', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ muted_channels: keys })
          });
          showToast(muted ? 'Creator muted' : 'Creator unmuted');
        } catch (error) {
          chip.classList.toggle('is-muted', !muted);
          showToast('Could not save that');
        }
      });

      /* ---- channel description ---- */
      document.addEventListener('click', (event) => {
        if (!event.target.closest('[data-ch-about-open]')) return;
        /* A dialog rather than an inline expand: the description sits in a grid cell beside
           the avatar, so growing it in place shoved the avatar down and left nothing to
           click to put it back. */
        document.querySelector('[data-ch-about-dialog]')?.showModal();
      });
      document.addEventListener('click', (event) => {
        /* Backdrop click. Only the dialog element itself can be the target here; anything
           inside it hits a child. */
        if (event.target.matches?.('dialog.ch-about-dialog')) event.target.close();
      });

      /* ---- channel search ---- */
      document.addEventListener('click', (event) => {
        const toggle = event.target.closest('[data-ch-search-toggle]');
        if (!toggle) return;
        const form = toggle.closest('[data-ch-search]');
        const input = form.querySelector('[data-ch-search-input]');
        if (form.classList.contains('is-open')) {
          /* Open with something typed means "search"; open and empty means "put it away". */
          if (input.value.trim()) { form.submit(); return; }
          form.classList.remove('is-open');
          toggle.setAttribute('aria-expanded', 'false');
          return;
        }
        form.classList.add('is-open');
        toggle.setAttribute('aria-expanded', 'true');
        input.focus();
      });
      document.addEventListener('focusout', (event) => {
        const form = event.target.closest('[data-ch-search]');
        if (!form || !form.classList.contains('is-open')) return;
        const input = form.querySelector('[data-ch-search-input]');
        if (input.value.trim()) return;
        /* Deferred so clicking the magnifier to close does not race this. */
        setTimeout(() => {
          if (form.contains(document.activeElement) || input.value.trim()) return;
          form.classList.remove('is-open');
          form.querySelector('[data-ch-search-toggle]')?.setAttribute('aria-expanded', 'false');
        }, 120);
      });

      /* ---- details popup ---- */
      document.addEventListener('click', (event) => {
        if (!event.target.closest('[data-open-details]')) return;
        event.preventDefault();
        document.querySelectorAll('.yt-menu[open]').forEach((menu) => menu.removeAttribute('open'));
        const dialog = document.querySelector('[data-details-dialog]');
        /* Forced open so the collapsed <details> inside is not a second thing to click. */
        dialog?.querySelector('.video-details')?.setAttribute('open', '');
        dialog?.showModal();
      });

      /* ---- status pane loads when opened ---- */
      (() => {
        let loaded = false;
        document.addEventListener('click', async (event) => {
          if (!event.target.closest('[data-settings-pane="status"]') || loaded) return;
          const body = document.querySelector('[data-status-body]');
          if (!body) return;
          loaded = true;
          try {
            body.innerHTML = await fetch('/api/status/panel').then((r) => r.text());
          } catch (error) {
            loaded = false;
            body.innerHTML = '<p class="dialog-note">Could not load status.</p>';
          }
        });
      })();

      /* ---- feed refresh ---- */
      document.addEventListener('click', async (event) => {
        const button = event.target.closest('[data-feed-refresh]');
        if (!button) return;
        event.preventDefault();
        button.disabled = true;
        const label = button.textContent;
        button.textContent = 'Refreshing\u2026';
        try {
          const data = await fetch('/api/feed/refresh', { method: 'POST' }).then((r) => r.json());
          showToast(`Feed rebuilt from ${data.profile_terms || 0} interests`, {
            label: 'See it', onClick: () => { window.location.href = '/'; },
          });
        } catch (error) {
          showToast('Could not refresh the feed');
        } finally {
          button.disabled = false;
          button.textContent = label;
        }
      });

      document.addEventListener('click', async (event) => {
        const retry = event.target.closest('[data-takeout-retry-subs]');
        if (!retry) return;
        event.preventDefault();
        retry.disabled = true;
        try {
          const data = await fetch('/api/takeout/subscriptions/retry', { method: 'POST' }).then((r) => r.json());
          showToast(`${data.cleared} channels queued to try again`);
        } catch (error) {
          showToast('Could not queue those channels');
        } finally {
          retry.disabled = false;
        }
      });

      /* ---- apply imported subscriptions ---- */
      document.addEventListener('click', async (event) => {
        const button = event.target.closest('[data-takeout-apply-subs]');
        if (!button) return;
        event.preventDefault();
        button.disabled = true;
        let total = 0;
        try {
          /* Batched server-side, so this loops until nothing is pending rather than
             creating several hundred subscriptions in one request. */
          for (let pass = 0; pass < 40; pass += 1) {
            const data = await fetch('/api/takeout/subscriptions/apply', { method: 'POST' }).then((r) => r.json());
            total += data.subscribed || 0;
            button.textContent = `Subscribed ${total}\u2026`;
            if (data.done || (!data.subscribed && !data.failed)) break;
          }
          showToast(`Subscribed to ${total} channels`);
        } catch (error) {
          showToast('Could not finish subscribing');
        } finally {
          button.disabled = false;
        }
      });

      /* ---- sponsorblock notification ---- */
      window.addEventListener('message', (event) => {
        if (event.origin !== window.location.origin) return;
        const data = event.data || {};
        if (data.type !== 'openfeed:sb-skipped') return;
        const player = document.querySelector('.player-frame');
        showToast(`Skipped ${data.label}`, {
          label: 'Undo',
          onClick: () => player?.contentWindow?.postMessage(
            { type: 'openfeed:sb-undo', seconds: data.undoTo }, window.location.origin),
        });
      });

      /* ---- description overflow ---- */
      (() => {
        const box = document.querySelector('[data-description-box]');
        if (!box) return;
        const body = box.querySelector('.watch-description-body');
        const toggle = box.querySelector('[data-description-toggle]');
        if (!body || !toggle) return;
        /* Only the browser knows whether the text actually overflows the collapsed
           height — the server cannot guess it from character count, and offering "More"
           on a description with nothing more to show is just a dead button. */
        function sync() {
          const overflows = body.scrollHeight > body.clientHeight + 2;
          toggle.hidden = !overflows;
          if (!overflows) box.classList.remove('is-collapsed');
        }
        sync();
        /* Re-measure when the column width changes, since rewrapping changes the height. */
        if (window.ResizeObserver) new ResizeObserver(() => {
          if (box.classList.contains('is-collapsed') || toggle.hidden) sync();
        }).observe(body);
        /* ---- pending details arriving ---- */
        /* The server renders the page from whatever the library holds, then fills
           gaps (description, views, publish date) in the background. When it said so
           via data-details-pending, poll our own details endpoint and swap the block
           once it lands -- same pattern as the dislike estimate, and it never reaches
           past our own database. */
        const watchLayout = document.querySelector('[data-watch-layout]');
        if (watchLayout && watchLayout.dataset.detailsPending) {
          const detailsVideoId = watchLayout.dataset.videoId || '';
          const meta = box.querySelector('.watch-description-meta');
          let attempts = 0;
          const timer = window.setInterval(async () => {
            attempts += 1;
            if (attempts > 10 || !detailsVideoId) { window.clearInterval(timer); return; }
            try {
              const data = await fetch(`/api/videos/${encodeURIComponent(detailsVideoId)}/details`).then((r) => r.json());
              if (!data.complete && !data.has_description) return;
              if (data.meta_html && meta) meta.innerHTML = data.meta_html;
              if (data.has_description && data.body_html) {
                body.innerHTML = data.body_html;
                sync();
              }
              if (data.complete) {
                delete watchLayout.dataset.detailsPending;
                window.clearInterval(timer);
              }
            } catch (error) {
              window.clearInterval(timer);
            }
          }, 3000);
        }
      })();

      /* ---- watch progress ---- */
      (() => {
        const layout = document.querySelector('[data-watch-layout]');
        if (!layout) return;
        const videoId = layout.dataset.videoId || '';
        const duration = Number(layout.dataset.duration || 0);
        if (!videoId) return;

        /* Say so when a video opens partway in, with a way back to the start — resuming
           silently looks like a bug the first time it happens to you. */
        const resumeAt = Number(layout.dataset.resume || 0);
        if (resumeAt > 0) {
          const mins = Math.floor(resumeAt / 60);
          const secs = Math.round(resumeAt % 60);
          showToast(`Resuming from ${mins}:${String(secs).padStart(2, '0')}`, {
            label: 'Start over',
            onClick: () => { window.location.href = `/watch/${encodeURIComponent(videoId)}?restart=1`; }
          });
        }

        /* The player is a cross-origin iframe, so its currentTime cannot be read. Time
           spent on the page with the tab visible is the closest honest proxy, and it is
           what lets opening a video count as watching it with no button to press. */
        let dwell = 0;
        let reported = 0;
        let last = Date.now();
        let marked = false;

        function tick() {
          const now = Date.now();
          if (document.visibilityState === 'visible') dwell += (now - last) / 1000;
          last = now;
        }

        function report(final) {
          tick();
          if (dwell < 5) return;
          /* Only the time since the last report is sent. current_seconds restarts at zero
             on every page open, so the server cannot difference it — a rewatch would
             subtract. Counted optimistically because a beacon cannot be confirmed, and a
             few lost seconds matter far less to ranking than double counting them. */
          const delta = Math.max(0, dwell - reported);
          reported = dwell;
          const body = JSON.stringify({
            youtube_video_id: videoId,
            current_seconds: duration ? Math.min(dwell, duration) : dwell,
            duration_seconds: duration || null,
            event_type: 'progress',
            watch_delta_seconds: delta,
            payload: { source: 'watch_dwell' }
          });
          if (final && navigator.sendBeacon) {
            navigator.sendBeacon('/api/playback/progress', new Blob([body], { type: 'application/json' }));
            return;
          }
          /* A dropped ping just means the next one carries the total — nothing to report. */
          fetch('/api/playback/progress', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body }).catch(() => {});
        }

        /* Videos whose length we never imported cannot produce a percentage, so they get
           marked watched outright once you have stayed long enough to have watched them. */
        function markUntimedWatched() {
          if (marked || duration || dwell < 60) return;
          marked = true;
          fetch('/api/feedback', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ youtube_video_id: videoId, event_type: 'mark_watched', payload: { source: 'watch_dwell' } })
          }).catch(() => { marked = false; });
        }

        setInterval(() => { report(false); markUntimedWatched(); }, 15000);
        document.addEventListener('visibilitychange', tick);
        window.addEventListener('pagehide', () => report(true));
      })();

      /* ---- per-channel notification level ---- */
      document.addEventListener('click', async (event) => {
        const choice = event.target.closest('[data-notify-level]');
        if (!choice) return;
        event.preventDefault();
        const menu = choice.closest('[data-notify-menu]');
        const channelId = choice.dataset.channelId || menu?.dataset.channelId || '';
        const level = choice.dataset.notifyLevel || 'all';
        if (!channelId) return;
        try {
          const response = await fetch(`/api/channels/${encodeURIComponent(channelId)}/notifications`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ level })
          });
          if (!response.ok) throw new Error('notify failed');
          const data = await response.json();
          applyNotifyLevel(menu, data.level || level);
          showToast(NOTIFY_TOASTS[data.level || level] || 'Notifications updated');
        } catch (error) {
          showToast('Could not update notifications');
        } finally {
          if (menu) menu.removeAttribute('open');
        }
      });

      function applyNotifyLevel(menu, level) {
        if (!menu) return;
        const trigger = menu.querySelector('[data-notify-trigger]');
        if (trigger) {
          const label = NOTIFY_LABELS[level] || 'Notifications';
          trigger.classList.toggle('is-muted', level === 'none');
          trigger.setAttribute('aria-label', `Notifications: ${label}`);
          trigger.setAttribute('title', `Notifications: ${label}`);
          const bell = trigger.firstElementChild;
          if (bell) bell.outerHTML = NOTIFY_ICONS[level] || NOTIFY_ICONS.all;
        }
        menu.querySelectorAll('[data-notify-level]').forEach((row) => {
          const trailing = row.querySelector('.menu-trailing');
          if (trailing) trailing.textContent = row.dataset.notifyLevel === level ? '\\u2713' : '';
        });
      }

      /* ---- horizontal shelves ---- */
      function syncShelf(shelf) {
        const track = shelf.querySelector('[data-shelf-track]');
        if (!track) return;
        const prev = shelf.querySelector('[data-shelf-prev]');
        const next = shelf.querySelector('[data-shelf-next]');
        const max = track.scrollWidth - track.clientWidth - 4;
        if (prev) prev.hidden = track.scrollLeft <= 4;
        if (next) next.hidden = track.scrollLeft >= max;
      }
      function bindShelf(shelf) {
        if (shelf.dataset.shelfBound) return;
        shelf.dataset.shelfBound = '1';
        const track = shelf.querySelector('[data-shelf-track]');
        if (!track) return;
        track.addEventListener('scroll', () => syncShelf(shelf), { passive: true });
        syncShelf(shelf);
      }
      function bindShelves(root) {
        (root || document).querySelectorAll('[data-shelf]').forEach(bindShelf);
      }
      bindShelves();
      window.addEventListener('resize', () => document.querySelectorAll('[data-shelf]').forEach(syncShelf));

      document.addEventListener('click', (event) => {
        const shelfButton = event.target.closest('[data-shelf-next], [data-shelf-prev]');
        if (shelfButton) {
          const shelf = shelfButton.closest('[data-shelf]');
          const track = shelf?.querySelector('[data-shelf-track]');
          if (!track) return;
          const step = Math.max(240, track.clientWidth - 40);
          track.scrollBy({ left: shelfButton.hasAttribute('data-shelf-prev') ? -step : step, behavior: 'smooth' });
          window.setTimeout(() => syncShelf(shelf), 420);
          return;
        }
        const dismiss = event.target.closest('[data-dismiss-shelf]');
        if (dismiss) {
          dismiss.closest('[data-feed-section]')?.remove();
          return;
        }
        const pageTab = event.target.closest('[data-page-tab]');
        if (pageTab) {
          event.preventDefault();
          const name = pageTab.dataset.pageTab;
          document.querySelectorAll('[data-page-tab]').forEach((tab) => tab.classList.toggle('active', tab === pageTab));
          document.querySelectorAll('[data-tab-panel]').forEach((panel) => { panel.hidden = panel.dataset.tabPanel !== name; });
          return;
        }
        const viewButton = event.target.closest('[data-set-view]');
        if (viewButton) {
          const view = viewButton.dataset.setView;
          document.querySelectorAll('[data-set-view]').forEach((button) => button.classList.toggle('active', button === viewButton));
          document.querySelectorAll('[data-view-target]').forEach((target) => {
            target.classList.toggle('view-list', view === 'list');
          });
          localStorage.setItem('openfeed-view', view);
          return;
        }
      });

      /* ---- client-side filtering on list pages ---- */
      document.querySelectorAll('[data-filter-input]').forEach((input) => {
        input.addEventListener('input', () => {
          const needle = input.value.trim().toLowerCase();
          document.querySelectorAll('[data-filter-item]').forEach((item) => {
            const haystack = (item.dataset.filterItem || item.textContent || '').toLowerCase();
            item.hidden = Boolean(needle) && !haystack.includes(needle);
          });
          document.querySelectorAll('[data-filter-group]').forEach((group) => {
            group.hidden = !group.querySelector('[data-filter-item]:not([hidden])');
          });
        });
      });

      const feedObserverTarget = document.querySelector('[data-feed-container]');
      if (feedObserverTarget && 'MutationObserver' in window) {
        new MutationObserver(() => bindShelves(feedObserverTarget)).observe(feedObserverTarget, { childList: true });
      }
    '''



def _app_header(search_value: str = "", user: dict[str, Any] | None = None, user_key: str = "local") -> str:
    return f'''
        <header class="app-header">
          <div class="header-left">
            <button class="icon-button nav-toggle" type="button" data-nav-toggle aria-label="Toggle navigation">{_icon("menu")}</button>
            <a class="brand" href="/" aria-label="OpenFeed home">{_brand_mark()}<span class="brand-word">Open<b>Feed</b></span></a>
          </div>
          <div class="header-center">
            <form class="search-form" action="/search" method="get" role="search">
              <input name="q" type="search" value="{escape(search_value, quote=True)}" placeholder="Search" aria-label="Search">
              <button type="submit" aria-label="Search">{_icon("search")}</button>
            </form>
          </div>
          {_header_actions(user, user_key)}
        </header>
    '''


def _source_offer() -> str:
    """AGPL-3.0 section 13 wants remote users pointed at the source they are running."""
    url = get_settings().source_url
    if not url:
        return ""
    return f'<p class="source-offer"><a href="{escape(url, quote=True)}" target="_blank" rel="noopener noreferrer">Source code &middot; AGPL-3.0</a></p>'


def _app_sidebar(active: str, meta_html: str) -> str:
    return f'''
        <aside class="desktop-sidebar" aria-label="Primary navigation">
          {_home_nav(active)}
          <div class="sidebar-meta">{meta_html}{_source_offer()}</div>
        </aside>
    '''


def _head_icons() -> str:
    """Favicon markup, in one place because there is more than one page template.

    Adding the links to a single head is how tabs ended up iconless everywhere except one
    route: the app shell, the standalone pages and the error page each build their own
    <head>. The player iframes deliberately do not call this — nothing shows a favicon for
    an embedded frame.
    """
    return (
        '<link rel="icon" type="image/png" sizes="32x32" href="/assets/favicon-32.png">'
        '<link rel="apple-touch-icon" sizes="180x180" href="/assets/apple-touch-icon.png">'
    )


def _asset_preconnect() -> str:
    """Warm the TLS handshakes for thumbnails and the fallback player up front."""
    hints = ['<link rel="preconnect" href="https://i.ytimg.com" crossorigin>']
    if get_settings().youtube_fallback_enabled:
        hints.append('<link rel="preconnect" href="https://www.youtube-nocookie.com" crossorigin>')
    return "".join(hints)


def _app_shell(title: str, active: str, content: str, search_value: str = "", main_class: str = "app-page", scripts: str = "", request: Request | None = None, immersive: bool = False) -> str:
    user = _current_user(request)
    user_key = _user_key(request)
    meta_html = f'<p>Private video library<br>Invidious-backed playback</p>'
    return f'''
    <!doctype html>
    <html lang="en">
      <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>{escape(title)} - OpenFeed</title>
        {_head_icons()}
        {_asset_preconnect()}
        <style>{_home_styles()}</style>
      </head>
      <body{' class="immersive-nav"' if immersive else ''}>
        <a class="skip-link" href="#main">Skip to content</a>
        {_app_header(search_value, user, user_key)}
        {_app_sidebar(active, meta_html)}
        <div class="nav-scrim" data-nav-scrim aria-hidden="true"></div>
        <main class="home-main {escape(main_class)}" id="main" tabindex="-1">{content}</main>
        <nav class="mobile-bottom-nav" aria-label="Mobile navigation">{_mobile_nav(active)}</nav>
        {_settings_dialog(user, user_key)}
        {_playlist_dialogs()}
        {_collection_dialogs()}
        <div class="feedback-toast" data-feedback-toast role="status" aria-live="polite"></div>
        <script>{_home_script()}</script>
        {scripts}
      </body>
    </html>
    '''


def _media_row(
    video: dict[str, Any],
    settings: Any,
    label: str = "",
    index: int | None = None,
    playlist_row_id: str | None = None,
    trailing: str = "",
) -> str:
    """One video as a horizontal row.

    `index`, `playlist_row_id`, and `trailing` exist so callers can extend the row
    without running str.replace over the markup this function returns. The playlist
    page used to do exactly that, matching the literal '<article class="media-row">' —
    which stopped matching the moment the data-* attributes were added here, silently
    dropping both the index cell and the row's identifying attribute. The CSS still
    reserved a column for the missing cell, so every remaining child shifted one column
    left and the thumbnail rendered 24px wide. Extension points beat string surgery on
    generated HTML.

    `playlist_row_id` is escaped inside this function; pass the raw id. `trailing` is
    the one raw-HTML slot and must only ever receive server-generated, pre-sanitized
    markup (the playlist remove control) — never anything derived from user input.
    """
    title_text = str(video.get("title") or "Untitled")
    title = escape(title_text)
    channel_text = str(video.get("channel_title") or video.get("author") or "Unknown channel")
    channel = escape(channel_text)
    video_id_raw = str(video.get("youtube_video_id") or "")
    video_id = escape(video_id_raw)
    watch_href = f"/watch/{video_id}" if video_id else "#"
    channel_id = escape(str(video.get("youtube_channel_id") or ""))
    channel_href = f"/channel/{channel_id}" if channel_id else "/channels"
    thumbnail = escape(_thumbnail_for(video))
    thumb_img = f'<img src="{thumbnail}" alt="" loading="lazy" onerror="this.remove()">' if thumbnail else ""
    thumb_fallback = escape(_avatar_initials(title_text))
    duration = escape(_duration_text(video.get("duration_seconds")))
    duration_html = f'<span class="duration">{duration}</span>' if duration else ""
    source_label = escape(label or _source_label(video))
    meta_parts = _video_meta_parts(video)
    latest_event = video.get("latest_event_at")
    if isinstance(latest_event, datetime):
        meta_parts.append(f"activity {_relative_time(latest_event)}")
    meta = " &middot; ".join(escape(part) for part in meta_parts) or source_label
    original_url = escape(video.get("url") or f"https://www.youtube.com/watch?v={video_id_raw}")
    invidious_url = escape(f"{settings.invidious_public_url}/watch?v={video_id_raw}") if video_id_raw and settings.invidious_public_url else ""
    menu = _video_action_menu(video_id_raw, title_text, watch_href, original_url, invidious_url)
    index_html = f'<span class="row-index">{int(index)}</span>' if index is not None else ""
    playlist_attr = (
        f' data-playlist-row="{escape(str(playlist_row_id), quote=True)}"'
        if playlist_row_id else ""
    )
    return f'''
    <article class="media-row"{playlist_attr} data-video-card data-video-id="{video_id}" data-channel-id="{channel_id}">
      {index_html}<a class="thumb" href="{watch_href}" aria-label="Watch {title}"><span class="thumb-fallback">{thumb_fallback}</span>{thumb_img}{duration_html}</a>
      <div class="media-row-copy">
        <h2><a href="{watch_href}">{title}</a></h2>
        <p><a href="{channel_href}">{channel}</a></p>
        <p>{meta}</p>
      </div>
      {menu}{trailing}
    </article>
    '''


def _subscription_posts_shelf(user_key: str = "local", limit: int = 6) -> str:
    """Recent community posts from subscribed channels, for the feeds.

    Bounded hard: each channel is one upstream request, so this reads a slice of the
    subscription list rather than all of it. A page render must not turn into seventy
    calls to Invidious.
    """
    from . import posts as posts_module

    try:
        subscriptions = db.list_subscriptions(200)
    except Exception:
        return ""
    channel_ids = [str(item.get("youtube_channel_id") or "") for item in subscriptions]
    channel_ids = [cid for cid in channel_ids if posts_module.valid_channel_id(cid)][:12]
    if not channel_ids:
        return ""
    try:
        found = posts_module.subscription_posts(channel_ids, per_channel=2, limit=12)[:limit]
    except Exception as exc:  # noqa: BLE001 - the video feed matters more than the shelf
        logger.info("subscription posts shelf failed: %s", exc)
        return ""
    if not found:
        return ""
    votes = {post["post_id"]: db.get_poll_vote(post["post_id"], user_key) for post in found}
    cards = "".join(_post_card(post, votes.get(post["post_id"]), compact=True) for post in found)
    return (
        '<section class="section-block post-shelf" aria-label="Community posts">'
        '<div class="section-heading"><h2>Posts from your subscriptions</h2></div>'
        f'<div class="post-list is-shelf">{cards}</div></section>'
    )


def _post_attachment_html(attachment: dict[str, Any] | None, post_id: str, vote: int | None) -> str:
    """Draw whatever the post carries, or say plainly that we cannot draw it."""
    if not attachment:
        return ""
    kind = attachment.get("type")

    if kind == "video":
        video_id = escape(str(attachment.get("youtube_video_id") or ""), quote=True)
        title = escape(str(attachment.get("title") or ""))
        duration = escape(_duration_text(attachment.get("duration_seconds")) or "")
        views = _compact_count(attachment.get("view_count"))
        meta = " &middot; ".join(part for part in (f"{escape(views)} views" if views else "", escape(attachment.get("author") or "")) if part)
        return (
            f'<a class="post-video" href="/watch/{video_id}">'
            f'<span class="post-video-thumb"><img src="/vi/{video_id}/hqdefault.jpg" alt="" loading="lazy">'
            f'{f"<span class=\'duration\'>{duration}</span>" if duration else ""}</span>'
            f'<span class="post-video-copy"><strong>{title}</strong><small>{meta}</small></span></a>'
        )

    if kind == "image":
        images = [escape(str(url), quote=True) for url in (attachment.get("images") or [])]
        cells = "".join(f'<img src="{url}" alt="" loading="lazy" onerror="this.remove()">' for url in images)
        multi = " is-multi" if len(images) > 1 else ""
        return f'<div class="post-images{multi}">{cells}</div>'

    if kind == "playlist":
        return (
            f'<a class="post-playlist" href="https://www.youtube.com/playlist?list={escape(str(attachment.get("playlist_id")), quote=True)}"'
            f' target="_blank" rel="noopener noreferrer">{_icon("playlist")}'
            f'<span><strong>{escape(str(attachment.get("title") or "Playlist"))}</strong>'
            f'<small>{escape(str(attachment.get("video_count") or ""))} videos</small></span></a>'
        )

    if kind == "poll":
        choices = attachment.get("choices") or []
        total = _compact_count(attachment.get("total_votes"))
        rows = []
        for index, choice in enumerate(choices):
            picked = vote == index
            rows.append(
                f'<button class="poll-choice{" is-picked" if picked else ""}" type="button" '
                f'data-poll-vote="{escape(post_id, quote=True)}" data-choice="{index}" '
                f'aria-pressed="{"true" if picked else "false"}">'
                f'<span class="poll-bar" aria-hidden="true"></span>'
                f'<span class="poll-text">{escape(str(choice))}</span>'
                f'<span class="poll-mark">{_icon("check")}</span></button>'
            )
        cleared = "" if vote is None else (
            f'<button class="link-action" type="button" data-poll-clear="{escape(post_id, quote=True)}">Clear my answer</button>'
        )
        return (
            f'<div class="post-poll" data-poll="{escape(post_id, quote=True)}">{"".join(rows)}'
            f'<p class="poll-note">{_icon("block")}<span><strong>Your answer stays here.</strong> '
            f'YouTube does not publish how a poll was split and does not accept votes without a signed-in '
            f'account, so this records what you picked locally and changes nothing on YouTube. '
            f'{f"{escape(total)} people voted there." if total else ""} {cleared}</span></p></div>'
        )

    return (
        f'<p class="post-unsupported">{_icon("hide")}<span>This post has a '
        f'{escape(str(attachment.get("raw_type") or "n unknown"))} attachment that OpenFeed cannot show yet. '
        f'The text above is complete.</span></p>'
    )


def _post_card(post: dict[str, Any], vote: int | None = None, *, compact: bool = False) -> str:
    """One community post."""
    post_id = str(post.get("post_id") or "")
    channel_id = str(post.get("youtube_channel_id") or "")
    author = str(post.get("author") or "Unknown")
    avatar = _avatar_html(author, post.get("author_thumbnail"))
    when = escape(post.get("published_text") or _relative_time(post.get("published_at")) or "")
    likes = _compact_count(post.get("like_count"))
    replies = _compact_count(post.get("reply_count"))
    edited = '<span class="post-flag">edited</span>' if post.get("is_edited") else ""
    body = post.get("content_html") or escape(str(post.get("content") or "")).replace("\n", "<br>")
    detail = f"/channel/{escape(channel_id, quote=True)}/posts/{escape(post_id, quote=True)}"
    meta = " &middot; ".join(part for part in (
        f'{likes} likes' if likes else "", f'{replies} replies' if replies else "") if part)
    return f'''
      <article class="post-card{" is-compact" if compact else ""}" data-post-id="{escape(post_id, quote=True)}">
        <header class="post-head">
          {avatar}
          <div><strong>{escape(author)}</strong><small>{when}{edited}</small></div>
          <a class="post-permalink" href="{detail}" aria-label="Open this post">{_icon("external")}</a>
        </header>
        <div class="post-body">{body}</div>
        {_post_attachment_html(post.get("attachment"), post_id, vote)}
        {f'<p class="post-meta">{meta}</p>' if meta else ""}
      </article>
    '''


def _channel_card(channel: dict[str, Any]) -> str:
    title_text = str(channel.get("title") or "Unknown channel")
    title = escape(title_text)
    channel_id = escape(str(channel.get("youtube_channel_id") or ""))
    href = f"/channel/{channel_id}" if channel_id else "/channels"
    video_count = int(channel.get("video_count") or 0)
    latest = _relative_time(channel.get("latest_video_at"))
    hue = _avatar_hue(title_text)
    initials = escape(_avatar_initials(title_text))
    latest_html = f"Latest {escape(latest)}" if latest else "No dated videos yet"
    return f'''
    <a class="channel-card" href="{href}" data-filter-item="{escape(title_text, quote=True)}">
      <div class="channel-card-top">
        {_avatar_html(title_text, channel.get("avatar_url") or channel.get("channel_avatar_url"))}
        <div>
          <h2>{title}</h2>
          <p>{video_count} videos</p>
          <p>{latest_html}</p>
        </div>
      </div>
    </a>
    '''


def _is_playlist_subscription(subscription: dict[str, Any]) -> bool:
    haystack = " ".join(str(subscription.get(key) or "") for key in ("id", "title", "feed_url", "html_url"))
    return "playlist" in haystack.lower() or "playlist_id=" in haystack.lower()


def _subscription_card(
    subscription: dict[str, Any],
    member_id: str | None = None,
    trailing: str = "",
) -> str:
    """One subscription as a channel card.

    `member_id` and `trailing` exist so collection_page can extend the card without
    running str.replace over the markup this function returns — the same string
    surgery _media_row grew out of. `member_id` is escaped inside this function;
    pass the raw id. `trailing` is the one raw-HTML slot and must only ever receive
    server-generated, pre-sanitized markup (the collection remove control) — never
    anything derived from user input.
    """
    title_text = str(subscription.get("title") or "Untitled subscription")
    title = escape(title_text)
    channel_id_raw = str(subscription.get("youtube_channel_id") or "")
    channel_id = escape(channel_id_raw)
    external_url = str(subscription.get("html_url") or subscription.get("feed_url") or "")
    href = f"/channel/{channel_id}" if channel_id else (escape(external_url) if external_url else "/channels")
    rel = ' rel="noreferrer"' if href.startswith(("http://", "https://")) else ""
    video_count = int(subscription.get("video_count") or 0)
    latest = _relative_time(subscription.get("latest_video_at"))
    latest_html = f"Latest {escape(latest)}" if latest else "No dated videos yet"
    feed_label = "Playlist" if _is_playlist_subscription(subscription) else "RSS feed"
    hue = _avatar_hue(title_text)
    initials = escape(_avatar_initials(title_text))
    feed_url_raw = str(subscription.get("feed_url") or "")
    subscribe_pill = ""
    if channel_id_raw and feed_url_raw:
        subscribe_pill = (
            f'<button class="subscribe-pill subscribed" type="button" data-unsubscribe-feed '
            f'data-channel-id="{channel_id}" data-feed-url="{escape(feed_url_raw, quote=True)}" '
            f'data-title="{escape(title_text, quote=True)}">{_icon("check")}<span>Subscribed</span></button>'
        )
    subscription_id = escape(str(subscription.get("id") or ""), quote=True)
    member_attr = (
        f' data-member-card="{escape(str(member_id), quote=True)}"'
        if member_id else ""
    )
    return f'''
    <article class="channel-card"{member_attr} data-subscription-card="{subscription_id}" data-channel-id="{channel_id}" data-filter-item="{escape(title_text, quote=True)}">
      <a class="channel-card-top" href="{href}"{rel}>
        {_avatar_html(title_text, subscription.get("channel_avatar_url"))}
        <div>
          <h2>{title}</h2>
          <p>{escape(feed_label)} &middot; {video_count} videos</p>
          <p>{latest_html}</p>
        </div>
      </a>
      <div class="card-actions-inline">{subscribe_pill}</div>
    {trailing}</article>
    '''


def _collection_cover(collection: dict[str, Any]) -> str:
    titles = collection.get("member_titles") or []
    avatars = collection.get("member_avatars") or []
    if not isinstance(titles, list):
        titles = []
    if not isinstance(avatars, list):
        avatars = []
    pairs = [(str(title), str(avatars[index]) if index < len(avatars) else "") for index, title in enumerate(titles) if title][:4]
    if not pairs:
        return f'<span class="collection-cover empty">{_icon("collections")}</span>'
    tiles = "".join(_avatar_html(title, avatar) for title, avatar in pairs)
    return f'<span class="collection-cover" data-tiles="{len(pairs)}">{tiles}</span>'


def _collection_card(collection: dict[str, Any]) -> str:
    name_text = str(collection.get("name") or "Untitled collection")
    name = escape(name_text)
    collection_id = escape(str(collection.get("id") or ""), quote=True)
    description_text = str(collection.get("description") or "")
    count = int(collection.get("subscription_count") or 0)
    latest = _relative_time(collection.get("latest_member_at"))
    meta = f"{count} channel{'' if count == 1 else 's'}"
    if latest:
        meta += f" · updated {latest}"
    href = f"/collection/{collection_id}"
    return f'''
    <article class="collection-card" data-collection-card="{collection_id}" data-filter-item="{escape(name_text, quote=True)}">
      <a class="collection-card-cover" href="{href}" aria-label="Open {name}">{_collection_cover(collection)}</a>
      <div class="collection-card-body">
        <div>
          <h3><a href="{href}">{name}</a></h3>
          <p>{escape(meta)}</p>
        </div>
        <details class="card-menu yt-menu">
          <summary class="menu-trigger" aria-label="Collection actions for {name}">{_icon("more")}</summary>
          <div class="menu-panel" role="menu" aria-label="Collection actions">
            {_menu_row("chevron-right", "Open collection", href=href)}
            {_menu_row("plus", "Add channels", attrs=f' data-add-channels="{collection_id}" data-collection-name="{escape(name_text, quote=True)}"')}
            {_menu_row("pencil", "Rename", attrs=f' data-edit-collection="{collection_id}" data-collection-name="{escape(name_text, quote=True)}" data-collection-description="{escape(description_text, quote=True)}"')}
            {_menu_row("trash", "Delete collection", attrs=f' data-delete-collection="{collection_id}"', danger=True)}
          </div>
        </details>
      </div>
    </article>
    '''


def _collection_dialogs() -> str:
    return f'''
      <dialog class="feed-dialog" id="collection-edit-dialog" aria-labelledby="collection-edit-title">
        <form id="collection-edit-form">
          <div class="dialog-head">
            <div>
              <h2 id="collection-edit-title">New collection</h2>
              <p>Group subscribed channels so you can browse them together.</p>
            </div>
            <button class="icon-button" type="button" data-close-dialog aria-label="Close">{_icon("x")}</button>
          </div>
          <div class="dialog-body">
            <label class="text-field">Name<input name="name" type="text" placeholder="Design &amp; code" required maxlength="120"></label>
            <label class="text-field">Description<input name="description" type="text" placeholder="Optional" maxlength="400"></label>
          </div>
          <div class="dialog-actions">
            <p class="status-line" data-collection-status></p>
            <button type="button" data-close-dialog>Cancel</button>
            <button class="primary-action" type="submit" data-collection-submit>Create</button>
          </div>
        </form>
      </dialog>
      <dialog class="feed-dialog" id="collection-channels-dialog" aria-labelledby="collection-channels-title">
        <div class="dialog-head">
          <div>
            <h2 id="collection-channels-title">Add channels</h2>
            <p data-channels-subtitle>Pick the subscriptions that belong in this collection.</p>
          </div>
          <button class="icon-button" type="button" data-close-dialog aria-label="Close">{_icon("x")}</button>
        </div>
        <div class="dialog-body">
          <label class="toolbar-search">{_icon("search")}<input type="search" placeholder="Search subscriptions" data-channels-filter aria-label="Filter subscriptions"></label>
          <div class="picker-list" data-channels-list><p class="picker-empty">Loading subscriptions…</p></div>
        </div>
        <div class="dialog-actions">
          <p class="status-line" data-channels-status></p>
          <button class="primary-action" type="button" data-close-dialog>Done</button>
        </div>
      </dialog>
      <dialog class="feed-dialog" id="channel-collections-dialog" aria-labelledby="channel-collections-title">
        <div class="dialog-head">
          <div>
            <h2 id="channel-collections-title">Add to collection</h2>
            <p data-channel-collections-subtitle>Choose the collections this channel belongs to.</p>
          </div>
          <button class="icon-button" type="button" data-close-dialog aria-label="Close">{_icon("x")}</button>
        </div>
        <div class="dialog-body">
          <div class="picker-list" data-channel-collections-list><p class="picker-empty">Loading collections…</p></div>
          <form class="picker-create" data-collection-quick-create>
            <input class="field-input" name="name" type="text" placeholder="New collection name" maxlength="120">
            <button class="primary-action" type="submit">Create &amp; add</button>
          </form>
        </div>
      </dialog>
    '''


@app.get("/collection/{collection_id}", response_class=HTMLResponse)
def collection_page(request: Request, collection_id: str, limit: int = 36):
    auth_redirect = _login_required_redirect(request)
    if auth_redirect:
        return auth_redirect
    if not _valid_uuid(collection_id):
        raise HTTPException(status_code=404, detail="Collection not found")
    settings = get_settings()
    user_key = _user_key(request)
    collection = db.get_subscription_collection(collection_id, user_key)
    if not collection:
        raise HTTPException(status_code=404, detail="Collection not found")

    members = db.list_collection_subscriptions(collection_id, user_key)
    videos = db.list_collection_videos(collection_id, max(1, min(limit, 120)), user_key)
    name_text = str(collection.get("name") or "Untitled collection")
    name = escape(name_text)
    collection_id_attr = escape(str(collection.get("id") or ""), quote=True)
    description_text = str(collection.get("description") or "")
    latest = _relative_time(collection.get("latest_member_at"))
    meta_parts = [f"{len(members)} channel{'' if len(members) == 1 else 's'}", f"{len(videos)} recent video{'' if len(videos) == 1 else 's'}"]
    if latest:
        meta_parts.append(f"updated {latest}")

    member_cards = []
    for member in members:
        member_id_raw = str(member.get("id") or "")
        title_text = str(member.get("title") or "Untitled subscription")
        remove = (
            f'<button class="collection-remove" type="button" data-remove-collection-member="{escape(member_id_raw, quote=True)}" '
            f'data-collection-id="{collection_id_attr}" aria-label="Remove {escape(title_text)} from {name}">{_icon("x")}</button>'
        )
        member_cards.append(_subscription_card(member, member_id=member_id_raw or None, trailing=remove))
    members_html = "".join(member_cards) or '<p class="empty-copy">No channels in this collection yet.</p>'
    videos_html = "".join(_home_video_card(video, index, settings) for index, video in enumerate(videos, start=1)) or '<p class="empty-copy">No videos from these channels yet. Run a sync to pull them in.</p>'
    description_html = f'<p class="collection-hero-description">{escape(description_text)}</p>' if description_text else ""

    content = f'''
      <section class="collection-hero">
        <div class="collection-hero-cover">{_collection_cover({"member_titles": [m.get("title") for m in members[:4]]})}</div>
        <div class="collection-hero-copy">
          <p class="collection-hero-kicker">Collection</p>
          <h1>{name}</h1>
          <p class="collection-hero-meta">{escape(" · ".join(meta_parts))}</p>
          {description_html}
          <div class="collection-hero-actions">
            <button class="primary-action" type="button" data-add-channels="{collection_id_attr}" data-collection-name="{escape(name_text, quote=True)}">{_icon("plus")}<span>Add channels</span></button>
            <button class="text-action" type="button" data-edit-collection="{collection_id_attr}" data-collection-name="{escape(name_text, quote=True)}" data-collection-description="{escape(description_text, quote=True)}">{_icon("pencil")}<span>Rename</span></button>
            <button class="text-action" type="button" data-delete-collection="{collection_id_attr}" data-redirect="/channels#collections">{_icon("trash")}<span>Delete</span></button>
          </div>
        </div>
      </section>

      <section class="section-block">
        <div class="section-heading"><h2>Channels</h2></div>
        <div class="channel-grid collection-members">{members_html}</div>
      </section>

      <section class="section-block">
        <div class="section-heading"><h2>Recent videos</h2></div>
        <div class="video-grid">{videos_html}</div>
      </section>
    '''
    return _app_shell(name_text, "channels", content, request=request)


def _playlist_card(playlist: dict[str, Any]) -> str:
    name_text = str(playlist.get("name") or "Untitled playlist")
    name = escape(name_text)
    playlist_id = escape(str(playlist.get("id") or ""), quote=True)
    description_text = str(playlist.get("description") or "")
    count = int(playlist.get("video_count") or 0)
    latest = _relative_time(playlist.get("latest_video_at"))
    meta = f"{count} video{'' if count == 1 else 's'}"
    if latest:
        meta += f" · updated {latest}"
    cover = escape(_thumbnail_for({
        "youtube_video_id": playlist.get("cover_youtube_video_id"),
        "thumbnail_url": playlist.get("cover_thumbnail_url"),
    }))
    cover_html = (
        f'<img src="{cover}" alt="" loading="lazy" onerror="this.remove()">'
        if cover else
        f'<span class="playlist-cover-empty">{_icon("playlist")}</span>'
    )
    href = f"/playlist/{playlist_id}"
    return f'''
    <article class="playlist-card" data-playlist-card="{playlist_id}" data-filter-item="{escape(name_text, quote=True)}">
      <a class="playlist-cover" href="{href}" aria-label="Open {name}">
        {cover_html}
        <span class="playlist-count">{_icon("playlist")}<span>{count}</span></span>
      </a>
      <div class="playlist-body">
        <div>
          <h3><a href="{href}">{name}</a></h3>
          <p>{escape(meta)}</p>
        </div>
        <details class="card-menu yt-menu">
          <summary class="menu-trigger" aria-label="Playlist actions for {name}">{_icon("more")}</summary>
          <div class="menu-panel" role="menu" aria-label="Playlist actions">
            {_menu_row("play", "Open playlist", href=href)}
            {_menu_row("pencil", "Rename", attrs=f' data-edit-playlist="{playlist_id}" data-playlist-name="{escape(name_text, quote=True)}" data-playlist-description="{escape(description_text, quote=True)}"')}
            {_menu_row("trash", "Delete playlist", attrs=f' data-delete-playlist="{playlist_id}"', danger=True)}
          </div>
        </details>
      </div>
    </article>
    '''


def _playlist_dialogs() -> str:
    return f'''
      <dialog class="feed-dialog" id="playlist-edit-dialog" aria-labelledby="playlist-edit-title">
        <form id="playlist-edit-form">
          <div class="dialog-head">
            <div>
              <h2 id="playlist-edit-title">New playlist</h2>
              <p>Playlists are private to your OpenFeed account.</p>
            </div>
            <button class="icon-button" type="button" data-close-dialog aria-label="Close">{_icon("x")}</button>
          </div>
          <div class="dialog-body">
            <label class="text-field">Name<input name="name" type="text" placeholder="Watch later" required maxlength="120"></label>
            <label class="text-field">Description<input name="description" type="text" placeholder="Optional" maxlength="400"></label>
          </div>
          <div class="dialog-actions">
            <p class="status-line" data-playlist-status></p>
            <button type="button" data-close-dialog>Cancel</button>
            <button class="primary-action" type="submit" data-playlist-submit>Create</button>
          </div>
        </form>
      </dialog>
      <dialog class="feed-dialog" id="playlist-picker-dialog" aria-labelledby="playlist-picker-title">
        <div class="dialog-head">
          <div>
            <h2 id="playlist-picker-title">Save to playlist</h2>
            <p data-picker-subtitle>Choose a playlist for this video.</p>
          </div>
          <button class="icon-button" type="button" data-close-dialog aria-label="Close">{_icon("x")}</button>
        </div>
        <div class="dialog-body">
          <div class="picker-list" data-picker-list><p class="picker-empty">Loading playlists…</p></div>
          <form class="picker-create" data-picker-create>
            <input class="field-input" name="name" type="text" placeholder="New playlist name" maxlength="120">
            <button class="primary-action" type="submit">Create &amp; add</button>
          </form>
        </div>
      </dialog>
    '''


def _feedback_script(video_id_json: str | None = None) -> str:
    scoped_video = f"const scopedVideoId = {video_id_json};" if video_id_json else "const scopedVideoId = null;"
    return f'''
    <script>
      {scoped_video}
      const toast = document.querySelector('[data-feedback-toast]');
      function showToast(message) {{
        if (!toast) return;
        toast.textContent = message;
        toast.classList.add('show');
        window.clearTimeout(showToast.timer);
        showToast.timer = window.setTimeout(() => toast.classList.remove('show'), 1800);
      }}
      document.querySelectorAll('[data-event], [data-feedback]').forEach((button) => {{
        button.addEventListener('click', async () => {{
          const videoId = button.dataset.videoId || scopedVideoId;
          const eventType = button.dataset.event || button.dataset.feedback;
          if (!videoId || !eventType) return;
          button.disabled = true;
          try {{
            const response = await fetch('/api/feedback', {{
              method: 'POST',
              headers: {{ 'Content-Type': 'application/json' }},
              body: JSON.stringify({{ youtube_video_id: videoId, event_type: eventType, payload: {{ source: 'ui' }} }})
            }});
            if (!response.ok) throw new Error('feedback failed');
            showToast('Saved');
          }} catch (error) {{
            showToast('Could not save');
          }} finally {{
            button.disabled = false;
          }}
        }});
      }});
      document.querySelectorAll('.tab').forEach((button) => {{
        button.addEventListener('click', () => {{
          document.querySelectorAll('.tab').forEach((tab) => tab.classList.toggle('active', tab === button));
          document.querySelectorAll('.tab-panel').forEach((panel) => panel.classList.toggle('active', panel.id === `tab-${{button.dataset.tab}}`));
        }});
      }});
    </script>
    '''
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    auth_redirect = _login_required_redirect(request)
    if auth_redirect:
        return auth_redirect
    settings = get_settings()
    user = _current_user(request)
    user_key = str(user.get("user_key") or "local")
    recommendations_preview = db.list_recommended_videos(40, 0, "all", user_key)
    library_videos = db.list_subscription_recommendations(24, user_key)
    all_for_channels = recommendations_preview + library_videos
    initial_sections = _home_feed_sections_html(0, "all", settings, user_key)
    if not initial_sections.strip():
        initial_sections = '<p class="empty-copy">No recommendations yet. Run a sync, then refresh this page.</p>'
    creator_strip = _creator_strip(all_for_channels)

    return f'''
    <!doctype html>
    <html lang="en">
      <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>OpenFeed</title>
        {_head_icons()}
        {_asset_preconnect()}
        <style>{_home_styles()}</style>
      </head>
      <body>
        {_app_header("", user, user_key)}
        {_app_sidebar("home", "")}
        <main class="home-main">
          <div class="topic-wrap" aria-label="Recommendation filters"><div class="topic-row">{_topic_chips(all_for_channels)}</div></div>
          <section class="creator-strip" aria-label="Channels">{creator_strip}</section>
          <div id="home-feed-sections" data-feed-container data-next-page="1" data-feed-filter="all">
            {initial_sections}
          </div>
          <div class="feed-loading" data-feed-loading hidden><span></span><span>Loading more recommendations</span></div>
          <div class="feed-retry" data-feed-retry hidden><span>Could not load more videos.</span><button type="button" data-retry-feed>Retry</button></div>
          <div class="feed-sentinel" data-feed-sentinel aria-hidden="true"></div>
        </main>
        <nav class="mobile-bottom-nav" aria-label="Mobile navigation">{_mobile_nav()}</nav>
        {_feed_dialog(user_key)}
        {_settings_dialog(user, user_key)}
        {_playlist_dialogs()}
        {_collection_dialogs()}
        <div class="feedback-toast" data-feedback-toast role="status" aria-live="polite"></div>
        <script>{_home_script()}</script>
      </body>
    </html>
    '''


@app.get("/recommendations", response_class=HTMLResponse, tags=["pages"], summary="Redirects to Home")
def recommendations_page():
    """Home already *is* the recommendation feed.

    This route returned the home page verbatim, so the sidebar appeared to offer two
    different feeds that were the same thing. Kept as a redirect so old links resolve.
    """
    return RedirectResponse(url="/", status_code=308)


@app.get("/watch/{youtube_video_id}", response_class=HTMLResponse)
def watch_video(request: Request, youtube_video_id: str):
    auth_redirect = _login_required_redirect(request)
    if auth_redirect:
        return auth_redirect
    user_key = _user_key(request)
    video = db.get_video_by_youtube_id(youtube_video_id)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    # Views alone was too narrow a trigger: a video acquired by channel backfill or topic
    # search arrives with a view count but no description and no like count, so the page
    # rendered "No description has been imported" forever and never tried again.
    # Dates get the same treatment: a decoded "2 days ago" timestamp reads as freshly
    # published and sorts wrong, and nothing re-read it on open either. A missing date
    # only triggers before first enrichment -- without that guard, a video whose source
    # genuinely has no date would refetch on every single open.
    needs_detail = (
        video.get("view_count") is None
        or not str(video.get("summary") or video.get("description") or "").strip()
        or video.get("like_count") is None
        or _published_date_is_decoded(video.get("published_at"))
        or (video.get("published_at") is None and video.get("metadata_fetched_at") is None)
    )
    if needs_detail and not video.get("metadata_error"):
        if video.get("view_count") is None:
            # Nothing to show at all: worth the wait, since the page would otherwise be
            # mostly empty.
            enrich_video(youtube_video_id, video.get("id"))
            video = db.get_video_by_youtube_id(youtube_video_id) or video
        else:
            # Partial data. Fill the gaps in the background rather than holding the page
            # open for an Invidious round trip on every single open.
            threading.Thread(
                target=enrich_video, args=(youtube_video_id, video.get("id")),
                name=f"enrich-{youtube_video_id}", daemon=True,
            ).start()

    settings = get_settings()
    # The page below renders whatever the library has right now; when enrichment
    # was just (re)triggered -- blocking or background -- the description, view
    # count or publish date can still arrive seconds later. The flag lets the
    # watch script poll our own database for the filled-in details and swap them
    # in, instead of showing "No description" until a manual reload. Likes keep
    # the old reload behavior: their slot may not exist in the markup yet.
    details_pending = (
        video.get("view_count") is None
        or not str(video.get("summary") or video.get("description") or "").strip()
        or _published_date_is_decoded(video.get("published_at"))
        or video.get("published_at") is None
    )
    details_pending_attr = ' data-details-pending="1"' if details_pending else ""
    title_text = str(video.get("title") or youtube_video_id)
    title = escape(title_text)
    channel_text = str(video.get("channel_title") or video.get("author") or "Unknown channel")
    channel = escape(channel_text)
    channel_id_raw = str(video.get("youtube_channel_id") or "").strip()
    channel_id = escape(channel_id_raw, quote=True)
    channel_href = f"/channel/{channel_id}" if channel_id else "/channels"
    youtube_url_raw = str(video.get("url") or f"https://www.youtube.com/watch?v={youtube_video_id}")
    youtube_url = escape(youtube_url_raw, quote=True)
    invidious_url = escape(f"{settings.invidious_public_url}/watch?v={youtube_video_id}", quote=True) if settings.invidious_public_url else youtube_url
    summary_text = _description_text(video.get("summary") or video.get("description") or "")
    summary = _rich_text(summary_text)
    published_relative = _relative_time(video.get("published_at"))
    published_date = _date_text(video.get("published_at"))
    published_text = escape(published_relative or published_date)
    published_detail = escape(published_date or published_relative or "Release date unavailable")
    views = _compact_count(video.get("view_count"))
    view_text = f"{escape(views)} views" if views else "Views unavailable"
    subscribers = _compact_count(video.get("channel_subscriber_count"))
    subscriber_text = f"{escape(subscribers)} subscribers" if subscribers else ""
    # Views and the release date live here rather than under the channel name — they
    # describe the video, not the person who made it.
    description_meta = " &middot; ".join(part for part in [view_text, published_text] if part)
    subscription = escape(video.get("subscription_title") or _source_label(video))
    read_state = "read" if video.get("is_read") else "unread"
    starred_state = "starred" if video.get("is_starred") else "not starred"
    metadata_source = escape(video.get("metadata_source") or "not fetched")
    duration_text = escape(_duration_text(video.get("duration_seconds")) or "unknown")
    captions_status = escape(video.get("captions_status") or "unknown")
    initials = escape(_avatar_initials(channel_text))
    hue = _avatar_hue(channel_text)
    subscription_feed_url_raw = _subscription_feed_for_video(video)
    feed_url_raw = subscription_feed_url_raw or (_youtube_feed_url(channel_id_raw) if channel_id_raw else "")
    subscribed = bool(subscription_feed_url_raw)
    # Where this video was left off, if a real player reading was ever stored for it. A
    # ?t= override in the URL wins, so "start over" is a link rather than extra state.
    progress_row = db.get_playback_progress(youtube_video_id, user_key) or {}
    stored_resume = int(float(progress_row.get("resume_seconds") or 0))
    restart = str(request.query_params.get("restart") or "") == "1"
    resume_at = 0 if restart else stored_resume
    resume_query = f"?t={resume_at}" if resume_at else ""
    resume_label = _duration_text(resume_at) if resume_at else ""

    feedback = db.get_video_feedback(youtube_video_id, user_key) or {}
    reaction = str(feedback.get("reaction") or "")
    like_selected = " is-selected" if reaction == "like" else ""
    dislike_selected = " is-selected" if reaction == "dislike" else ""
    like_pressed = "true" if reaction == "like" else "false"
    dislike_pressed = "true" if reaction == "dislike" else "false"
    # YouTube publishes likes but not dislikes, so the dislike side stays blank rather
    # than pretending a zero is a real count.
    like_total = _compact_count(video.get("like_count"))
    dislike_raw = video.get("dislike_count")
    dislike_total = _compact_count(dislike_raw) if dislike_raw else ""
    like_count_html = f'<span class="reaction-count" data-like-count>{escape(like_total)}</span>' if like_total else ""
    # An RYD figure is an estimate reconstructed by a third party, not a number YouTube
    # published, and the tooltip says so rather than presenting it as fact.
    dislike_from_ryd = str(video.get("dislike_source") or "") == "ryd"
    dislike_title = (
        "Estimated by Return YouTube Dislike — not an official count"
        if dislike_from_ryd
        else "Dislike count as published before YouTube removed them in 2021"
    )
    # Nothing stored yet: start a cover batch that includes this video so the count can
    # appear shortly, without a lone request announcing what is being watched.
    dislike_pending = False
    # `not dislike_raw` rather than `is None`: YouTube's own metadata reports 0 for every
    # video now, and treating that as a real count meant the fetch never fired.
    if not dislike_raw and not video.get("dislike_fetched_at"):
        dislike_pending = ryd.schedule_cover_batch(youtube_video_id, user_key)

    dislike_count_html = (
        f'<span class="reaction-count{" is-estimate" if dislike_from_ryd else ""}" data-dislike-count '
        f'title="{escape(dislike_title, quote=True)}">{escape(dislike_total)}</span>'
        if dislike_total
        else (
            f'<span class="reaction-count muted" data-dislike-count data-dislike-pending="{escape(youtube_video_id, quote=True)}" '
            f'title="Fetching an estimate&hellip;">&mdash;</span>'
            if dislike_pending
            else '<span class="reaction-count muted" data-dislike-count title="YouTube stopped publishing dislike counts in 2021. '
                 'Estimates can be enabled in Settings.">&mdash;</span>'
        )
    )
    metadata = video.get("metadata") if isinstance(video.get("metadata"), dict) else {}
    transcript_segments = metadata.get("transcript_segments") if isinstance(metadata, dict) else None
    has_transcript = isinstance(transcript_segments, list) and bool(transcript_segments)
    db.record_video_event(youtube_video_id, "watch_page_open", {"source": "watch_page", "weight": 0}, user_key)
    db.update_playback_progress(youtube_video_id, 0, video.get("duration_seconds"), "open", {"source": "watch_page"}, user_key)

    # Ranked against the video on screen and with shorts left out - they have their own
    # vertical viewer, and a 30-second clip is not a sensible follow-on from a long video.
    related = db.list_related_videos(youtube_video_id, 14, user_key)
    suggestion_items = [_media_row(candidate, settings, _source_label(candidate)) for candidate in related]
    suggestions_html = "".join(suggestion_items) or '<p class="chat-placeholder">No suggestions available yet.</p>'
    transcript_button = ""
    transcript_panel = ""
    if has_transcript:
        transcript_rows = []
        for segment in transcript_segments[:500]:
            start = escape(str(segment.get("start") or segment.get("time") or ""))
            text = escape(str(segment.get("text") or ""))
            if text:
                transcript_rows.append(f'<button class="transcript-row" type="button" data-seek="{start}"><span>{start}</span><p>{text}</p></button>')
        if transcript_rows:
            transcript_button = '<button class="tab" type="button" data-tab="transcript">Transcript</button>'
            transcript_panel = f'<section id="tab-transcript" class="tab-panel">{"".join(transcript_rows)}</section>'
    description_body = f'<p>{summary}</p>' if summary else '<p class="description-empty">No description has been imported for this video yet.</p>'
    details_html = _video_details_pane(video, download=db.get_download(youtube_video_id, user_key))
    # Hidden until the script measures the text; showing it up front means it flashes on
    # short descriptions that never needed it.
    description_html = f'''<section class="watch-description is-collapsed" data-description-box aria-label="Video description">
            <p class="watch-description-meta" title="Released {published_detail}">{description_meta}</p>
            <div class="watch-description-body">{description_body}</div>
            <button class="description-toggle" type="button" data-description-toggle data-more-label="More" data-less-label="Show less" hidden>More</button>
          </section>'''
    notify_level = db.get_channel_notification(channel_id_raw, user_key) if subscribed and channel_id_raw else "all"
    subscribe_button = _subscribe_control(channel_id_raw, feed_url_raw, channel_text, subscribed, notify_level)

    short_class = " is-short" if db.is_short_video(video) else ""
    show_comments = False
    try:
        show_comments = bool(db.get_feed_preferences(_user_key(request)).get("show_comments"))
    except Exception:
        show_comments = False
    comments_html = (
        f'<section class="comments" data-comments-for="{escape(youtube_video_id)}">'
        '<div class="comments-head"><h2>Comments</h2><span class="comments-count" data-comments-count></span></div>'
        '<div class="comments-body" data-comments-body>'
        '<p class="comments-state">Loading comments&hellip;</p>'
        '</div></section>'
    ) if show_comments else ""

    content = f'''
      <div class="watch-layout" data-watch-layout data-video-id="{escape(youtube_video_id)}"{details_pending_attr} data-duration="{int(video.get("duration_seconds") or 0)}" data-resume="{resume_at}">
        <div class="watch-stage">
          <div class="watch-player-shell{short_class}">
            <iframe class="player-frame" src="/watch/{escape(youtube_video_id)}/player{resume_query}" title="Invidious player" allow="autoplay; fullscreen; picture-in-picture" allowfullscreen></iframe>
          </div>
        </div>
        <article class="watch-content">
          <h1 class="watch-title">{title}</h1>
          <div class="watch-meta-line">
            <div class="watch-channel">
              {_avatar_html(channel_text, video.get("channel_avatar_url"), tag="a", href=channel_href)}
              <div>
                {_creator_links(video, channel_href)}
                <p class="metadata">{subscriber_text or subscription}</p>
              </div>
            </div>
            <div class="watch-inline-actions">{subscribe_button}</div>
            <div class="watch-actions" aria-label="Video actions">
            <div class="watch-reactions">
              <button class="pill-action reaction-action has-count{like_selected}" type="button" data-feedback="like" data-video-id="{escape(youtube_video_id)}" aria-label="Like{f' ({like_total} likes)' if like_total else ''}" title="Like" aria-pressed="{like_pressed}">{_icon("like")}{like_count_html}<span class="sr-only">Like</span></button>
              <button class="pill-action reaction-action has-count{dislike_selected}" type="button" data-feedback="dislike" data-video-id="{escape(youtube_video_id)}" aria-label="Dislike" title="Dislike" aria-pressed="{dislike_pressed}">{_icon("dislike")}{dislike_count_html}<span class="sr-only">Dislike</span></button>
            </div>
            <button class="pill-action" type="button" data-copy-url="/watch/{escape(youtube_video_id)}" aria-label="Copy share link" title="Share">{_icon("share")}<span>Share</span></button>
            <button class="pill-action" type="button" data-save-to-playlist="{escape(youtube_video_id)}" data-video-title="{escape(_text_excerpt(title_text, 90), quote=True)}">{_icon("playlist")}<span>Save</span></button>
            {_watch_options_menu(youtube_video_id, invidious_url, youtube_url, title_text)}
            </div>
          </div>
          {description_html}
          {f'<dialog class="feed-dialog details-dialog" data-details-dialog><div class="fc-head"><div><p class="fc-kicker">Video</p><h2>Details</h2></div><button class="icon-button" type="button" data-close-dialog aria-label="Close">{_icon("x")}</button></div><div class="fc-body">{details_html}</div></dialog>' if details_html else ""}
          {comments_html}
        </article>
        <aside class="watch-panel" aria-label="Watch side panel">
          {f'<div class="tabbar" role="tablist" aria-label="Watch panels"><button class="tab active" type="button" data-tab="suggestions">Up next</button>{transcript_button}</div>' if transcript_button else '<div class="section-heading"><h2>Up next</h2></div>'}
          <section id="tab-suggestions" class="tab-panel active">{suggestions_html}</section>
          {transcript_panel}
        </aside>
      </div>
      {_download_dialog()}
    '''
    return _app_shell(title_text, "home", content, main_class="watch-main", request=request, immersive=True)


# Per-source health. A source that fails goes into a cooldown that doubles each time
# (30s, 60s, 120s ... capped at 10 minutes); when the cooldown expires the source is
# simply tried again, so a backend that recovers gets used again with no intervention.
_SOURCE_COOLDOWN_BASE = 30.0
_SOURCE_COOLDOWN_MAX = 600.0
_source_health: dict[str, dict[str, float]] = {}
_source_health_lock = threading.Lock()


def _source_state(source_id: str) -> dict[str, float]:
    return _source_health.setdefault(source_id, {"failures": 0.0, "cooldown_until": 0.0, "last_ok": 0.0, "last_checked": 0.0})


def mark_source_result(source_id: str, ok: bool) -> None:
    """Record a real playback outcome for one source."""
    if not source_id:
        return
    now = time.time()
    with _source_health_lock:
        state = _source_state(source_id)
        state["last_checked"] = now
        if ok:
            state["failures"] = 0.0
            state["cooldown_until"] = 0.0
            state["last_ok"] = now
            return
        state["failures"] += 1
        backoff = min(_SOURCE_COOLDOWN_BASE * (2 ** (state["failures"] - 1)), _SOURCE_COOLDOWN_MAX)
        state["cooldown_until"] = now + backoff


def source_in_cooldown(source_id: str) -> bool:
    state = _source_health.get(source_id)
    return bool(state and time.time() < state["cooldown_until"])


def source_health_snapshot(source_id: str) -> dict[str, Any]:
    state = _source_health.get(source_id)
    now = time.time()
    if not state:
        return {"status": "unknown", "failures": 0, "cooldown_seconds": 0, "last_ok": None, "last_checked": None}
    cooling = max(0.0, state["cooldown_until"] - now)
    return {
        "status": "cooling-down" if cooling else ("healthy" if state["last_ok"] else "unknown" if not state["failures"] else "recovering"),
        "failures": int(state["failures"]),
        "cooldown_seconds": int(cooling),
        "last_ok": int(now - state["last_ok"]) if state["last_ok"] else None,
        "last_checked": int(now - state["last_checked"]) if state["last_checked"] else None,
    }


_PROBE_TTL_OK = 300.0
_PROBE_TTL_BAD = 45.0
_BACKEND_TRIP_AFTER = 3
_BACKEND_TRIP_SECONDS = 90.0
_probe_cache: dict[str, tuple[float, bool, str]] = {}
_probe_lock = threading.Lock()
_backend_state: dict[str, float] = {"failures": 0.0, "degraded_until": 0.0}


def playback_backend_degraded() -> bool:
    """True while Invidious is being skipped outright after repeated failures."""
    return time.time() < _backend_state["degraded_until"]


def _record_probe_result(ok: bool) -> None:
    with _probe_lock:
        if ok:
            _backend_state["failures"] = 0.0
            _backend_state["degraded_until"] = 0.0
            return
        _backend_state["failures"] += 1
        if _backend_state["failures"] >= _BACKEND_TRIP_AFTER:
            # Every video is failing, so stop paying a probe round trip per play and
            # send viewers straight to the fallback until the backend recovers.
            _backend_state["degraded_until"] = time.time() + _BACKEND_TRIP_SECONDS


def _embed_playable(youtube_video_id: str, use_cache: bool = True) -> tuple[bool, str]:
    """Check whether Invidious can actually serve a player for this video.

    Invidious answers with its full error page (site chrome, search box, footer) when
    playback is unavailable — usually because the companion's PO token was rejected by
    YouTube. Rendering that inside our player frame looks broken, so probe first.

    Results are cached, and once the backend has failed repeatedly the probe is skipped
    entirely: waiting on a request that is certain to fail is the single biggest delay
    before the fallback player can start.
    """
    settings = get_settings()
    if not settings.invidious_base_url:
        return False, "Invidious is not configured."

    now = time.time()
    if use_cache:
        cached = _probe_cache.get(youtube_video_id)
        if cached and cached[0] > now:
            return cached[1], cached[2]
        if playback_backend_degraded():
            return False, "The playback backend is not serving streams right now."

    timeout = httpx.Timeout(max(2.0, settings.playback_probe_timeout_seconds), connect=2.5)
    try:
        response = _invidious_client().get(
            f"{settings.invidious_base_url}/embed/{youtube_video_id}",
            timeout=timeout,
            follow_redirects=True,
        )
        body = response.text
        ok = response.status_code == 200 and "Video unavailable" not in body and "<video" in body
        reason = "" if ok else "Invidious could not load this video's streams."
    except Exception as exc:
        logger.warning("embed probe failed for %s: %s", youtube_video_id, exc)
        ok, reason = False, "The playback backend did not respond."

    _record_probe_result(ok)
    _probe_cache[youtube_video_id] = (now + (_PROBE_TTL_OK if ok else _PROBE_TTL_BAD), ok, reason)
    if len(_probe_cache) > 512:
        for key, value in list(_probe_cache.items()):
            if value[0] <= now:
                _probe_cache.pop(key, None)
        while len(_probe_cache) > 512:
            _probe_cache.pop(next(iter(_probe_cache)), None)
    return ok, reason


def _all_playback_sources(user_key: str = "local", downloaded: bool = False) -> list[dict[str, Any]]:
    """Every source this user *could* play from, enabled or not, in their chosen order.

    Disabled sources are still returned so Settings can list them with the switch off —
    a source you cannot see is a source you cannot turn back on.
    """
    settings = get_settings()
    preferences = db.get_playback_preferences(user_key)
    disabled = set(preferences.get("disabled_sources") or [])
    sources: list[dict[str, Any]] = []

    if settings.invidious_base_url:
        sources.append({"id": "invidious", "label": "Invidious (local)", "kind": "invidious-local", "base": "",
                        "hint": "Your own instance — private, but depends on YouTube accepting its token."})

    for base in list(preferences.get("public_invidious_urls") or []) + settings.invidious_fallback_list:
        host = urllib.parse.urlparse(base).netloc or base
        source_id = f"invidious:{host}"
        if any(source["id"] == source_id for source in sources):
            continue
        sources.append({"id": source_id, "label": host, "kind": "invidious-public", "base": base,
                        "hint": "Someone else's Invidious instance — it sees what you watch."})

    # A file already on disk beats every network source: it always works, it is the
    # quality the user chose, and it needs nothing from YouTube. Only offered for videos
    # that actually have one, so this entry appears and disappears per video.
    if downloaded:
        sources.append({"id": "downloaded", "label": "Downloaded copy", "kind": "downloaded", "base": "",
                        "hint": "Plays the file saved in your library — works offline and never touches YouTube."})

    if settings.ytdlp_enabled and settings.ytdlp_playback_fallback and ytdlp.available():
        sources.append({"id": "ytdlp", "label": "Direct stream (yt-dlp)", "kind": "ytdlp", "base": "",
                        "hint": "Extracts the stream itself and proxies it through OpenFeed — private, "
                                "but limited to the muxed rendition, usually 360p or 720p."})

    if settings.youtube_fallback_enabled:
        sources.append({"id": "youtube", "label": "YouTube", "kind": "youtube", "base": "",
                        "hint": "youtube-nocookie.com embed — the most reliable option."})

    for source in sources:
        source["enabled"] = source["id"] not in disabled

    # Apply the order the user dragged into place. Anything they have not ranked keeps
    # its natural position at the end, so a new instance never disappears.
    order = preferences.get("source_order") or []
    if order:
        rank = {source_id: index for index, source_id in enumerate(order)}
        sources.sort(key=lambda source: rank.get(source["id"], len(rank) + 1))

    # A file already on disk is not a preference call — it always works, it is the quality
    # the user picked, and it reaches YouTube not at all. A saved source order would
    # otherwise bury it at the bottom, since it is not a source anyone ever ranked.
    sources.sort(key=lambda source: 0 if source["kind"] == "downloaded" else 1)
    return sources


def _configured_playback_sources(user_key: str = "local", downloaded: bool = False) -> list[dict[str, Any]]:
    """The enabled sources only, in preference order, before health is applied."""
    return [source for source in _all_playback_sources(user_key, downloaded) if source.get("enabled", True)]


def _source_url(source: dict[str, str], video_id: str, play: str, start: int = 0) -> str:
    """Where to point the iframe, including the resume point.

    Each backend spells the start offset differently, and getting it into the URL is what
    makes the video open at the right place instead of seeking there a moment later.
    """
    if source["kind"] == "invidious-local":
        return f"/embed/{video_id}?autoplay={play}" + (f"&t={start}" if start else "")
    if source["kind"] == "invidious-public":
        return f"{source['base']}/embed/{video_id}?autoplay={play}" + (f"&t={start}" if start else "")
    if source["kind"] == "downloaded":
        return f"/watch/{video_id}/local?autoplay={play}" + (f"&t={start}" if start else "")
    if source["kind"] == "ytdlp":
        return f"/watch/{video_id}/ytdlp?autoplay={play}" + (f"&t={start}" if start else "")
    return (
        f"https://www.youtube-nocookie.com/embed/{video_id}?autoplay={play}&rel=0&modestbranding=1&playsinline=1"
        + (f"&start={start}" if start else "")
    )


def _playback_sources(youtube_video_id: str, autoplay: bool = True, user_key: str = "local", start: int = 0, downloaded: bool = False) -> tuple[list[dict[str, str]], str]:
    """The ordered list of players to try for one video.

    Order is the user's preference, re-sorted so sources currently in cooldown sink to
    the bottom instead of being dropped: a cooled-down source is still better than no
    player at all, and trying it again is exactly how it gets marked healthy once more.
    """
    video_id = urllib.parse.quote(youtube_video_id, safe="")
    play = "1" if autoplay else "0"
    configured = _configured_playback_sources(user_key, downloaded)
    if not configured:
        return [], "No playback source is enabled in settings."

    reason = ""
    demoted: set[str] = set()
    for source in configured:
        if source["id"] == "invidious":
            # The local instance is the only one we can probe cheaply and honestly.
            playable, probe_reason = _embed_playable(youtube_video_id)
            if not playable:
                demoted.add(source["id"])
                reason = probe_reason
        elif source_in_cooldown(source["id"]):
            demoted.add(source["id"])

    healthy = [source for source in configured if source["id"] not in demoted]
    unhealthy = [source for source in configured if source["id"] in demoted]

    ordered: list[dict[str, str]] = []
    for source in healthy + unhealthy:
        ordered.append({
            "id": source["id"],
            "label": source["label"],
            "kind": source["kind"],
            "url": _source_url(source, video_id, play, start),
            "same_origin": "1" if source["kind"] in {"invidious-local", "ytdlp", "downloaded"} else "",
        })
    return ordered, reason


@app.get("/watch/{youtube_video_id}/player", response_class=HTMLResponse)
def watch_player(request: Request, youtube_video_id: str, t: int = 0, autoplay: int = 1):
    """The player shell.

    `autoplay=0` is what makes Shorts preloading possible: the frame fetches the player
    and buffers, but waits for an openfeed:play message before it starts.
    """
    auth_redirect = _login_required_redirect(request)
    if auth_redirect:
        return auth_redirect
    video_id = escape(youtube_video_id)
    public = get_settings().invidious_public_url
    invidious_url = f"{public}/watch?v={video_id}" if public else ""
    youtube_url = f"https://www.youtube.com/watch?v={video_id}"
    start = max(0, min(int(t or 0), 86400))
    user_key = _user_key(request)
    downloaded = bool(db.get_download(youtube_video_id, user_key))
    sources, reason = _playback_sources(youtube_video_id, autoplay=bool(autoplay), user_key=user_key, start=start, downloaded=downloaded)
    playable = bool(sources)

    shell_styles = """
          html, body { margin: 0; width: 100%; height: 100%; background: #000; color: #ededef;
            font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Inter", "Segoe UI", Roboto, system-ui, sans-serif;
            letter-spacing: -.006em; }
          iframe { width: 100%; height: 100%; border: 0; background: #000; display: block; }
          /* Top-right so it never covers the player's own controls (fullscreen sits bottom-right). */
          .popout { position: absolute; top: 10px; right: 10px; width: 30px; height: 30px; display: grid; place-items: center;
            background: rgba(10,10,11,.62); border: 1px solid rgba(255,255,255,.14); border-radius: 8px;
            opacity: 0; transition: opacity .16s ease, background .16s ease; backdrop-filter: blur(6px); }
          body:hover .popout, .popout:focus-within { opacity: .85; }
          .popout:hover { opacity: 1; background: rgba(10,10,11,.85); }
          .popout a { color: #ededef; width: 100%; height: 100%; display: grid; place-items: center; text-decoration: none; }
          .popout svg { width: 15px; height: 15px; fill: none; stroke: currentColor; stroke-width: 1.75;
            stroke-linecap: round; stroke-linejoin: round; }
    """

    if playable:
        body_html = f'''
        <div class="stage" data-player-stage data-sources="{escape(json.dumps(sources), quote=True)}" data-video-id="{video_id}" data-fallback-reason="{escape(reason, quote=True)}">
          <img class="poster" src="/vi/{video_id}/hqdefault.jpg" alt="" aria-hidden="true">
          <div class="spinner" data-player-spinner aria-hidden="true"></div>
        </div>
        <div class="source-note" data-source-note hidden><span data-source-label></span><button type="button" data-retry-preferred>Retry Invidious</button></div>
        <div class="popout"><a href="{invidious_url or youtube_url}" target="_blank" rel="noreferrer" aria-label="Open in Invidious" title="Open in Invidious">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M15 3h6v6"/><path d="M10 14 21 3"/><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/></svg>
        </a></div>
        '''
        extra_styles = """
          .stage { position: absolute; inset: 0; background: #000; }
          .stage iframe { position: absolute; inset: 0; z-index: 2; }
          .poster { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover;
            opacity: .35; filter: blur(1px); transition: opacity .2s ease; }
          .stage.is-ready .poster, .stage.is-ready .spinner { opacity: 0; }
          .spinner { position: absolute; top: 50%; left: 50%; width: 26px; height: 26px; margin: -13px 0 0 -13px;
            border-radius: 999px; border: 2px solid rgba(255,255,255,.18); border-top-color: rgba(255,255,255,.7);
            animation: spin .8s linear infinite; transition: opacity .2s ease; }
          @keyframes spin { to { transform: rotate(360deg); } }
          @media (prefers-reduced-motion: reduce) { .spinner { animation-duration: 2.4s; } }
          .source-note { position: absolute; left: 10px; bottom: 10px; z-index: 3; display: flex; align-items: center; gap: 8px;
            padding: 6px 8px 6px 11px; border-radius: 999px; background: rgba(10,10,11,.72); border: 1px solid rgba(255,255,255,.14);
            backdrop-filter: blur(6px); font-size: 11.5px; color: #c9c9d1; opacity: 0; transition: opacity .18s ease; }
          body:hover .source-note, .source-note:focus-within { opacity: 1; }
          .source-note[hidden] { display: none; }
          .source-note button { border: 0; border-radius: 999px; background: rgba(255,255,255,.12); color: #ededef;
            font: inherit; font-size: 11px; padding: 3px 9px; cursor: pointer; }
          .source-note button:hover { background: rgba(255,255,255,.2); }
        """
    else:
        invidious_button = (
            f'<a class="action" href="{invidious_url}" target="_blank" rel="noreferrer">Open in Invidious</a>'
            if invidious_url else ""
        )
        diagnosis = _diagnose_playback(youtube_video_id)
        remedies = "".join(f"<li>{escape(item)}</li>" for item in diagnosis.get("remedies") or [])
        remedy_html = f"<ul class='remedies'>{remedies}</ul>" if remedies else ""
        chain = "".join(
            f'<span class="hop {"ok" if hop["ok"] else "bad"}">{escape(hop["name"])}</span>'
            for hop in diagnosis.get("steps") or []
        )
        chain_html = f'<div class="chain" aria-label="Playback chain">{chain}</div>' if chain else ""
        body_html = f'''
        <div class="unavailable" role="alert">
          <svg class="glyph" viewBox="0 0 24 24" aria-hidden="true"><path d="m2 2 20 20"/><path d="M10.5 5.2A9 9 0 0 1 22 12c0 1.5-.4 2.9-1.1 4.1"/><path d="M3.3 7.7A9 9 0 0 0 12 21a8.9 8.9 0 0 0 5-1.5"/></svg>
          <h1>{escape(diagnosis.get("headline") or "Can't play this video right now")}</h1>
          <p>{escape(diagnosis.get("explanation") or reason)}</p>
          {chain_html}
          {remedy_html}
          <div class="actions">
            <button class="action primary" type="button" onclick="location.reload()">Try again</button>
            {invidious_button}
            <a class="action" href="{youtube_url}" target="_blank" rel="noreferrer">Open on YouTube</a>
          </div>
        </div>
        '''
        extra_styles = """
          body { display: grid; place-items: center; background: #0a0a0b; overflow: auto; }
          .unavailable { max-width: 520px; padding: 32px 24px; text-align: center; }
          .chain { display: flex; flex-wrap: wrap; gap: 6px; justify-content: center; margin: 0 0 14px; }
          .hop { display: inline-flex; align-items: center; gap: 6px; padding: 3px 9px; border-radius: 999px;
            border: 1px solid rgba(255,255,255,.1); font-size: 11px; color: #8b8b93; }
          .hop::before { content: ''; width: 6px; height: 6px; border-radius: 999px; background: currentColor; }
          .hop.ok { color: #3fb950; }
          .hop.bad { color: #e5484d; border-color: rgba(229,72,77,.35); }
          .remedies { margin: 0 0 18px; padding: 0; list-style: none; display: grid; gap: 6px; text-align: left; }
          .remedies li { position: relative; padding-left: 18px; color: #c9c9d1; font-size: 12px; line-height: 1.55; }
          .remedies li::before { content: '\\2192'; position: absolute; left: 0; color: #6a6a72; }
          .glyph { width: 30px; height: 30px; fill: none; stroke: #8b8b93; stroke-width: 1.6;
            stroke-linecap: round; stroke-linejoin: round; margin-bottom: 14px; }
          .unavailable h1 { margin: 0 0 8px; font-size: 15px; font-weight: 600; letter-spacing: -.014em; }
          .unavailable p { margin: 0 0 18px; color: #8b8b93; font-size: 12.5px; line-height: 1.6; }
          .actions { display: flex; gap: 8px; justify-content: center; flex-wrap: wrap; }
          .action { min-height: 32px; display: inline-flex; align-items: center; padding: 0 14px; border: 0;
            border-radius: 999px; background: #1a1a1d; color: #ededef; font: inherit; font-size: 12.5px;
            font-weight: 500; cursor: pointer; text-decoration: none; }
          .action:hover { background: #232327; }
          .action.primary { background: #f1f1f3; color: #0a0a0b; font-weight: 600; }
        """

    preconnect = (
        '<link rel="preconnect" href="https://www.youtube-nocookie.com" crossorigin>'
        '<link rel="preconnect" href="https://i.ytimg.com" crossorigin>'
        '<link rel="dns-prefetch" href="https://googlevideo.com">'
        if get_settings().youtube_fallback_enabled else ""
    )
    script = f"<script>{_player_script()}</script>" if playable else ""
    return f"""
    <!doctype html>
    <html lang="en">
      <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        {preconnect}
        <style>{shell_styles}{extra_styles}</style>
      </head>
      <body>
        {body_html}
        {script}
      </body>
    </html>
    """


def _player_script() -> str:
    """Walks the playback ladder without the viewer having to do anything.

    The Invidious frame is same-origin (it is proxied through OpenFeed), so this can watch
    the real <video> element rather than guess: no <video> in the document, a media
    error, or nothing playing before the deadline all mean "try the next source".
    """
    return '''
      (() => {
        const stage = document.querySelector('[data-player-stage]');
        if (!stage) return;
        let sources = [];
        try { sources = JSON.parse(stage.dataset.sources || '[]'); } catch (error) { sources = []; }
        if (!sources.length) return;
        const VIDEO_ID = stage.dataset.videoId || '';

        const note = document.querySelector('[data-source-note]');
        const noteLabel = document.querySelector('[data-source-label]');
        const spinner = document.querySelector('[data-player-spinner]');
        const START_DEADLINE = 9000;
        const LOAD_DEADLINE = 12000;
        let index = 0;
        let frame = null;
        let timer = null;
        let settled = false;

        function clearTimer() { if (timer) { window.clearTimeout(timer); timer = null; } }

        const reported = new Set();
        function report(source, ok) {
          /* One report per source per video: health should track outcomes, not retries. */
          const key = `${source.id}:${ok}`;
          if (reported.has(key)) return;
          reported.add(key);
          try {
            const body = JSON.stringify({ source_id: source.id, ok, youtube_video_id: stage.dataset.videoId || '' });
            navigator.sendBeacon
              ? navigator.sendBeacon('/api/playback/report', new Blob([body], { type: 'application/json' }))
              : fetch('/api/playback/report', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body, keepalive: true });
          } catch (error) { /* health is best-effort */ }
        }

        function markReady(source) {
          settled = true;
          clearTimer();
          report(source, true);
          stage.classList.add('is-ready');
          if (spinner) spinner.hidden = true;
          if (note && noteLabel) {
            /* Say so whenever playback is not coming from the local Invidious, and say why. */
            const preferred = source.id === 'invidious';
            note.hidden = preferred;
            const why = stage.dataset.fallbackReason || '';
            const base = preferred ? '' : `Playing via ${source.label}${why ? ' — ' + why : ''}`;
            noteLabel.textContent = base;
            if (!source.same_origin) {
              /* Nothing inside a third-party player is reachable from here: we cannot read
                 its clock, so we cannot skip anything or draw a marker on its scrubber.
                 Saying that is better than a SponsorBlock toggle that silently does
                 nothing on exactly the videos that needed the fallback. */
              sbFetch().then((segments) => {
                if (!segments.length || noteLabel.textContent !== base) return;
                noteLabel.textContent = `${base} — SponsorBlock cannot skip in this player`;
              });
            }
          }
        }

        function next(why) {
          if (settled) return;
          clearTimer();
          report(sources[index], false);
          if (index + 1 >= sources.length) { markReady(sources[index]); return; }
          index += 1;
          console.info('OpenFeed player: switching source', why, '->', sources[index].id);
          mount();
        }

        /* Position reporting. The local Invidious embed and the yt-dlp player are both
           served through this origin, so their playhead is readable — that real reading is
           what gets stored as the resume point. A cross-origin embed is opaque, and rather
           than guess it simply reports nothing and the video will not offer to resume. */
        function trackPosition(video) {
          let lastSent = 0;
          function send(final) {
            const at = Number(video.currentTime || 0);
            if (!at || Math.abs(at - lastSent) < 5) return;
            lastSent = at;
            const body = JSON.stringify({
              youtube_video_id: VIDEO_ID,
              resume_seconds: at,
              duration_seconds: Number(video.duration) || null,
              event_type: 'position',
              payload: { source: 'player' }
            });
            if (final && navigator.sendBeacon) {
              navigator.sendBeacon('/api/playback/progress', new Blob([body], { type: 'application/json' }));
              return;
            }
            fetch('/api/playback/progress', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body }).catch(() => {});
          }
          video.addEventListener('timeupdate', () => send(false));
          video.addEventListener('pause', () => send(false));
          /* Leaving the page is exactly when the mark matters most, so it is also sent on
             the way out — a reload or a back button would otherwise lose the last stretch. */
          video.addEventListener('ended', () => send(false));
          window.addEventListener('pagehide', () => send(true));
          document.addEventListener('visibilitychange', () => {
            if (document.visibilityState === 'hidden') send(true);
          });
        }

        /* Timestamps in the description and in comments seek the player. The watch page is
           the parent frame and same-origin, but going through postMessage keeps the reach
           one-way: the page asks, this shell decides whether it can honour it. */
        let seekTarget = null;
        window.addEventListener('message', (event) => {
          if (event.origin !== window.location.origin) return;
          const data = event.data || {};
          if (data.type !== 'openfeed:seek') return;
          const seconds = Number(data.seconds);
          if (!isFinite(seconds) || seconds < 0) return;
          if (seekTarget) {
            seekTarget.currentTime = seconds;
            const play = seekTarget.play();
            if (play && play.catch) play.catch(() => {});
          }
        });

        /* Shorts preload the next few players muted and paused, then hand control here
           when one scrolls into view. Without this a preloaded frame would either play
           unheard in the background or have to be reloaded on arrival, which is the delay
           the preloading exists to remove. */
        window.addEventListener('message', (event) => {
          if (event.origin !== window.location.origin) return;
          const type = (event.data || {}).type;
          if (type === 'openfeed:sb-undo') {
            const seconds = Number((event.data || {}).seconds);
            if (seekTarget && isFinite(seconds)) {
              sbLastSkip = null;
              seekTarget.dataset.sbUndone = String(seconds);
              seekTarget.currentTime = seconds;
            }
            return;
          }
          if (type !== 'openfeed:play' && type !== 'openfeed:pause') return;
          if (!seekTarget) return;
          if (type === 'openfeed:pause') { seekTarget.pause(); return; }
          seekTarget.muted = false;
          const play = seekTarget.play();
          if (play && play.catch) play.catch(() => {
            /* Autoplay with sound can be refused; falling back to muted still means the
               picture moves the instant the Short arrives. */
            seekTarget.muted = true;
            const retry = seekTarget.play();
            if (retry && retry.catch) retry.catch(() => {});
          });
        });

        /* Two jobs, both needing a stylesheet inside the embed document.

           Invidious paints bright red markers on the seek bar for the segments it knows
           about; that is internal bookkeeping, not something worth putting in front of a
           viewer, so it is hidden.

           The SponsorBlock marks and the manual skip button are also created *inside* the
           embed, which does not load the app stylesheet — so the rules for them have to
           travel here too. Without this the marks are in the DOM with correct positions
           and no height, colour or positioning, which is exactly as visible as nothing.
           The embed is same-origin, which is the only reason any of this is possible.

           Invidious sizes its fluid player to 82vh instead of the 16:9 ratio, which
           pillarboxes every video inside our exactly-16:9 shell — restoring the ratio
           makes the video reach the shell edges the way YouTube's does.

           Its chrome is also much heavier than YouTube's: a solid control bar and a
           solid title card fading in on hover reads as the whole video dimming and
           undimming. YouTube-style gradients keep the controls readable without that. */
        const EMBED_CSS = `
          .vjs-marker { display: none !important; }
          .player-dimensions.vjs-fluid, .video-js.vjs-fluid { padding-top: 56.25% !important; }
          .video-js .vjs-control-bar { background: linear-gradient(transparent, rgba(0, 0, 0, .65)) !important; }
          .video-js .vjs-overlay { background: linear-gradient(rgba(0, 0, 0, .55), transparent) !important;
            padding: 12px 16px 30px !important; }
          .video-js .vjs-overlay h1 { font-size: 15px !important; line-height: 1.35 !important;
            margin: 0 !important; font-weight: 600 !important; }
          .video-js .vjs-overlay h1 a { color: #fff !important; text-decoration: none !important; }
          .video-js:hover .vjs-big-play-button, .video-js .vjs-big-play-button { background-color: rgba(35, 35, 35, .5) !important; }
          .sb-marks { position: absolute; left: 0; right: 0; top: 0; bottom: 0;
            pointer-events: none; z-index: 4; }
          .sb-mark { position: absolute; top: 0; height: 100%; min-width: 2px;
            background: #22c55e; opacity: .9; border-radius: 1px; }
          .sb-mark[data-category="selfpromo"] { background: #eab308; }
          .sb-mark[data-category="interaction"] { background: #a855f7; }
          .sb-mark[data-category="intro"], .sb-mark[data-category="outro"] { background: #38bdf8; }
          .sb-mark[data-category="preview"] { background: #f97316; }
          .sb-mark[data-category="filler"] { background: #94a3b8; }
          .sb-skip { position: absolute; right: 16px; bottom: 68px; z-index: 6;
            display: inline-flex; align-items: center; padding: 9px 14px; border: 0;
            border-radius: 8px; background: rgba(10,10,11,.86); color: #fff;
            font: 600 13px/1 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, system-ui, sans-serif;
            cursor: pointer; backdrop-filter: blur(6px); }
          .sb-skip[hidden] { display: none; }
          .sb-skip:hover { background: rgba(10,10,11,.96); }
        `;

        function styleEmbed(doc) {
          if (!doc || doc.getElementById('openfeed-embed-style')) return;
          const style = doc.createElement('style');
          style.id = 'openfeed-embed-style';
          style.textContent = EMBED_CSS;
          (doc.head || doc.documentElement).appendChild(style);
        }

        /* --- SponsorBlock ------------------------------------------------------------
           Segments are fetched once per video from our own backend, which does the hashed
           lookup. Skipping is driven off timeupdate rather than timers because a seek or a
           speed change would desynchronise anything scheduled ahead of time. */
        let sbSegments = [];
        let sbLastSkip = null;

        /* The "Skipped ... / Undo" notice belongs to the watch page, not to this shell,
           which is a bare iframe with no room to show it — so a skip posts a message up
           and the page raises its own toast. */

        function sbSkipButton(doc, segment) {
          let button = doc.querySelector('.sb-skip');
          if (!button) {
            button = doc.createElement('button');
            button.type = 'button';
            button.className = 'sb-skip';
            (doc.body || doc.documentElement).appendChild(button);
          }
          button.textContent = `Skip ${segment.label.toLowerCase()}`;
          button.hidden = false;
          button.onclick = () => {
            if (seekTarget) seekTarget.currentTime = segment.end;
            button.hidden = true;
          };
          return button;
        }

        /* Fetched once per shell, not once per source, and deliberately *before* we know
           whether the player will end up same-origin: if it does not, the segments are
           still worth having so the note can admit that nothing will be skipped. */
        let sbLoad = null;

        function sbFetch() {
          if (!sbLoad) {
            sbLoad = !VIDEO_ID ? Promise.resolve([]) :
              fetch(`/api/sponsorblock/${encodeURIComponent(VIDEO_ID)}`)
                .then((r) => r.json())
                .then((data) => { sbSegments = (data && data.segments) || []; return sbSegments; })
                .catch(() => []);
          }
          return sbLoad;
        }

        function sbAttach(doc, video) {
          sbFetch().then((segments) => {
            if (!segments.length) return;
            sbDrawMarks(doc, video);
            video.addEventListener('timeupdate', () => sbTick(doc, video));
          });
        }

        function sbDrawMarks(doc, video) {
          const duration = Number(video.duration) || 0;
          if (!duration) { video.addEventListener('loadedmetadata', () => sbDrawMarks(doc, video), { once: true }); return; }
          /* The scrubber itself, not the control that contains it: the holder is the 3px
             bar, so the marks line up with it exactly instead of floating in the 30px
             hit area around it. */
          const bar = doc.querySelector('.vjs-progress-holder') || doc.querySelector('.vjs-progress-control');
          let strip = doc.querySelector('.sb-marks');
          if (!strip || (bar && strip.parentElement !== bar)) {
            strip?.remove();
            strip = doc.createElement('div');
            strip.className = 'sb-marks';
            (bar || doc.body || doc.documentElement).appendChild(strip);
          }
          strip.textContent = '';
          for (const segment of sbSegments) {
            const mark = doc.createElement('span');
            mark.className = 'sb-mark';
            mark.dataset.category = segment.category;
            mark.style.left = `${(segment.start / duration) * 100}%`;
            mark.style.width = `${Math.max(0.4, ((segment.end - segment.start) / duration) * 100)}%`;
            mark.title = segment.label;
            strip.appendChild(mark);
          }
        }

        function sbTick(doc, video) {
          const at = Number(video.currentTime) || 0;
          const undone = Number(video.dataset.sbUndone || -1);
          const current = sbSegments.find((s) => at >= s.start && at < s.end - 0.2);
          const button = doc.querySelector('.sb-skip');
          if (!current) { if (button) button.hidden = true; return; }
          /* An undone skip stays undone until playback leaves the segment. */
          if (undone >= current.start && undone < current.end) return;
          if (current.action === 'skip') {
            if (sbLastSkip === current.uuid) return;
            sbLastSkip = current.uuid;
            const from = video.currentTime;
            video.currentTime = current.end;
            window.parent.postMessage({ type: 'openfeed:sb-skipped', label: current.label, undoTo: from }, window.location.origin);
          } else if (current.action === 'manual') {
            sbSkipButton(doc, current);
          }
        }

        function watchSameOrigin() {
          let doc = null;
          try { doc = frame.contentDocument; } catch (error) { doc = null; }
          if (!doc) return false;
          styleEmbed(doc);
          const video = doc.querySelector('video');
          if (!video) { next('no video element'); return true; }
          seekTarget = video;
          trackPosition(video);
          sbAttach(doc, video);
          const ready = () => markReady(sources[index]);
          video.addEventListener('playing', ready, { once: true });
          video.addEventListener('timeupdate', ready, { once: true });
          video.addEventListener('loadeddata', ready, { once: true });
          video.addEventListener('error', () => next('media error'), { once: true });
          clearTimer();
          timer = window.setTimeout(() => next('never started'), START_DEADLINE);
          return true;
        }

        function mount() {
          const source = sources[index];
          settled = false;
          if (frame) frame.remove();
          if (spinner) spinner.hidden = false;
          stage.classList.remove('is-ready');
          frame = document.createElement('iframe');
          frame.title = `${source.label} player`;
          frame.allow = 'autoplay; fullscreen; picture-in-picture; encrypted-media';
          frame.setAttribute('allowfullscreen', '');
          frame.addEventListener('error', () => next('frame error'));
          frame.addEventListener('load', () => {
            if (source.same_origin) {
              if (!watchSameOrigin()) markReady(source);
            } else {
              /* Cross-origin players cannot be inspected; a clean load is the signal. */
              markReady(source);
            }
          });
          frame.src = source.url;
          stage.appendChild(frame);
          clearTimer();
          timer = window.setTimeout(() => next('load timeout'), LOAD_DEADLINE);
        }

        const retry = document.querySelector('[data-retry-preferred]');
        const invidiousIndex = sources.findIndex((source) => source.id === 'invidious');
        if (retry && invidiousIndex < 0) retry.hidden = true;
        retry?.addEventListener('click', () => {
          index = Math.max(0, invidiousIndex);
          if (note) note.hidden = true;
          mount();
        });

        mount();
      })();
    '''


@app.api_route("/embed/{path:path}", methods=["GET", "HEAD"])
def proxy_invidious_embed(path: str, request: Request):
    return proxy_invidious(request, f"embed/{path}")


@app.api_route("/companion/{path:path}", methods=["GET", "HEAD"])
def proxy_invidious_companion(path: str, request: Request):
    return proxy_invidious(request, f"companion/{path}")


@app.api_route("/videojs/{path:path}", methods=["GET", "HEAD"])
def proxy_invidious_videojs(path: str, request: Request):
    return proxy_invidious(request, f"videojs/{path}")


@app.api_route("/css/{path:path}", methods=["GET", "HEAD"])
def proxy_invidious_css(path: str, request: Request):
    return proxy_invidious(request, f"css/{path}")


@app.api_route("/js/{path:path}", methods=["GET", "HEAD"])
def proxy_invidious_js(path: str, request: Request):
    return proxy_invidious(request, f"js/{path}")


@app.api_route("/ggpht/{path:path}", methods=["GET", "HEAD"])
def proxy_invidious_ggpht(path: str, request: Request):
    """Channel avatars and banners, proxied so the browser never calls Google directly.

    Buffered rather than streamed: Invidious intermittently answers these paths with an
    empty 200 (text/html). Passing that through lets the browser cache a broken image, so
    the artwork stays missing until the cache expires. Verify we actually got an image and
    fail loudly instead, letting the page fall back to initials/gradient on this render and
    retry on the next one.
    """
    auth_redirect = _login_required_redirect(request)
    if auth_redirect:
        return auth_redirect
    settings = get_settings()
    if not settings.invidious_base_url:
        raise HTTPException(status_code=502, detail="Invidious is not configured")
    url = f"{settings.invidious_base_url}/ggpht/{path.lstrip('/')}"
    if request.url.query:
        url = f"{url}?{request.url.query}"
    try:
        with _invidious_client().stream(
            request.method, url, headers=_invidious_proxy_headers(request),
            timeout=20.0, follow_redirects=True,
        ) as upstream:
            content_type = (upstream.headers.get("content-type") or "").lower()
            status_code = upstream.status_code
            if request.method == "HEAD":
                body = b""
            else:
                body = bytearray()
                for chunk in upstream.iter_bytes():
                    body.extend(chunk)
                    if len(body) > 5 * 1024 * 1024:
                        return Response(status_code=502, headers={"cache-control": "no-store"})
                body = bytes(body)
    except httpx.HTTPError as exc:
        return Response(status_code=502, headers={"cache-control": "no-store"}, content=str(exc)[:200])

    if status_code != 200 or not content_type.startswith("image/") or (request.method != "HEAD" and not body):
        return Response(status_code=502, headers={"cache-control": "no-store"})
    return Response(
        content=body,
        media_type=content_type.split(";")[0],
        headers={"cache-control": "public, max-age=86400, stale-while-revalidate=604800"},
    )


@app.api_route("/vi/{path:path}", methods=["GET", "HEAD"])
def proxy_invidious_vi(path: str, request: Request):
    return proxy_invidious(request, f"vi/{path}")


@app.api_route("/api/v1/storyboards/{path:path}", methods=["GET", "HEAD"])
def proxy_invidious_storyboards(path: str, request: Request):
    return proxy_invidious(request, f"api/v1/storyboards/{path}")


@app.api_route("/api/v1/annotations/{path:path}", methods=["GET", "HEAD"])
def proxy_invidious_annotations(path: str, request: Request):
    return proxy_invidious(request, f"api/v1/annotations/{path}")



@app.get("/auth/status")
def auth_status(request: Request) -> dict[str, Any]:
    user = _current_user(request)
    settings = get_settings()
    return {
        "ok": True,
        "authenticated": bool(user.get("authenticated")),
        "authorized": _user_has_required_access(user),
        "required_group": settings.required_group,
        "user": {key: user.get(key) for key in ("display_name", "email", "username", "sub", "groups")},
        "auth_mode": settings.auth_mode,
        "auth_configured": settings.auth_configured,
        "require_login": settings.require_login,
        "login_url": "/auth/login",
        "logout_url": "/auth/logout",
    }


@app.get("/auth/login")
def auth_login(request: Request, next: str = "/") -> Response:
    canonical_redirect = _canonical_login_redirect(request, next)
    if canonical_redirect:
        return canonical_redirect
    settings = get_settings()
    if settings.auth_mode != "authentik_oidc":
        return _auth_error_page("OpenFeed login is disabled", "Set OPENFEED_AUTH_MODE=authentik_oidc to enable Authentik login.", status_code=404)
    missing = _auth_missing_envs()
    if missing:
        return _auth_error_page("OpenFeed Authentik login is not configured", "Create or assign an Authentik OIDC provider for OpenFeed, then set these environment variables.", missing)
    try:
        discovery = _oidc_discovery_document()
    except HTTPException as exc:
        return _auth_error_page("OpenFeed Authentik login is unavailable", str(exc.detail), status_code=exc.status_code)
    authorization_endpoint = str(discovery.get("authorization_endpoint") or "")
    if not authorization_endpoint:
        return _auth_error_page("OpenFeed Authentik login is unavailable", "The OIDC discovery document did not include an authorization endpoint.")
    state_value = secrets.token_urlsafe(32)
    params = {
        "response_type": "code",
        "client_id": settings.oidc_client_id,
        "redirect_uri": _oidc_callback_url(request),
        "scope": settings.oidc_scope,
        "state": state_value,
    }
    response = RedirectResponse(f"{authorization_endpoint}?{urllib.parse.urlencode(params)}", status_code=status.HTTP_303_SEE_OTHER)
    _set_auth_state_cookie(response, {"state": state_value, "next": _safe_next_path(next), "created_at": _utc_timestamp(), "expires_at": _utc_timestamp() + AUTH_STATE_TTL_SECONDS})
    return response


@app.get("/auth/callback")
def auth_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
) -> Response:
    settings = get_settings()
    if settings.auth_mode != "authentik_oidc":
        return _auth_error_page("OpenFeed login is disabled", "Authentik login is not enabled for OpenFeed.", status_code=404)
    if error:
        detail = str(error_description or error).replace("+", " ")
        response = _auth_error_page("OpenFeed Authentik login was rejected", f"Authentik returned {error}: {detail}", status_code=400)
        _clear_auth_state_cookie(response)
        return response
    if not code and not state:
        response = RedirectResponse("/auth/login?next=/", status_code=status.HTTP_303_SEE_OTHER)
        _clear_auth_state_cookie(response)
        return response
    if not code or not state:
        return _auth_error_page("OpenFeed Authentik callback is invalid", "Authentik did not return the expected code and state.", status_code=400)
    pending = _auth_state_from_request(request)
    if not pending or not hmac.compare_digest(str(pending.get("state") or ""), state):
        response = _auth_error_page("OpenFeed Authentik callback is invalid", "The login state is missing, expired, or does not match.", status_code=400)
        _clear_auth_state_cookie(response)
        return response
    try:
        discovery = _oidc_discovery_document()
        token_endpoint = str(discovery.get("token_endpoint") or "")
        userinfo_endpoint = str(discovery.get("userinfo_endpoint") or "")
        if not token_endpoint or not userinfo_endpoint:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="oidc_endpoints_unavailable")
        token_response = httpx.post(
            token_endpoint,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": _oidc_callback_url(request),
                "client_id": settings.oidc_client_id,
                "client_secret": settings.oidc_client_secret,
            },
            headers={"Accept": "application/json"},
            timeout=10.0,
        )
        token_response.raise_for_status()
        token_payload = token_response.json()
        access_token = str(token_payload.get("access_token") or "")
        if not access_token:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="oidc_token_missing")
        userinfo_response = httpx.get(userinfo_endpoint, headers={"Accept": "application/json", "Authorization": f"Bearer {access_token}"}, timeout=10.0)
        userinfo_response.raise_for_status()
        userinfo = userinfo_response.json()
    except HTTPException as exc:
        response = _auth_error_page("OpenFeed Authentik login failed", str(exc.detail), status_code=exc.status_code)
        _clear_auth_state_cookie(response)
        return response
    except Exception as exc:
        response = _auth_error_page("OpenFeed Authentik login failed", str(exc), status_code=403)
        _clear_auth_state_cookie(response)
        return response
    if not isinstance(userinfo, dict):
        response = _auth_error_page("OpenFeed Authentik login failed", "Authentik returned invalid userinfo.", status_code=403)
        _clear_auth_state_cookie(response)
        return response
    sub = str(userinfo.get("sub") or "").strip()
    if not sub:
        response = _auth_error_page("OpenFeed Authentik login failed", "Authentik did not return an immutable subject identifier.", status_code=403)
        _clear_auth_state_cookie(response)
        return response
    email = str(userinfo.get("email") or "").strip().lower()
    name = str(userinfo.get("name") or userinfo.get("preferred_username") or email or "OpenFeed user").strip()
    groups = _auth_groups_from_userinfo(userinfo)
    candidate_user = {"authenticated": True, "groups": groups}
    if not _user_has_required_access(candidate_user):
        response = _access_denied_response()
        _clear_auth_session_cookie(response)
        _clear_auth_state_cookie(response)
        return response
    db.upsert_user(sub, name, {"email": email, "username": userinfo.get("preferred_username") or "", "groups": groups, "source": "authentik"})
    response = RedirectResponse(_safe_next_path(str(pending.get("next") or "/")), status_code=status.HTTP_303_SEE_OTHER)
    _set_auth_session_cookie(response, {"sub": sub, "email": email, "name": name, "username": str(userinfo.get("preferred_username") or ""), "groups": groups, "created_at": _utc_timestamp(), "expires_at": _utc_timestamp() + AUTH_SESSION_TTL_SECONDS})
    _clear_auth_state_cookie(response)
    return response


@app.api_route("/auth/logout", methods=["GET", "POST"])
def auth_logout(next: str = "/") -> Response:
    response = RedirectResponse(_safe_next_path(next), status_code=status.HTTP_303_SEE_OTHER)
    _clear_auth_session_cookie(response)
    _clear_auth_state_cookie(response)
    return response


@app.get("/search", response_class=HTMLResponse)
def search_page(request: Request, q: str = "", limit: int = 40, page: int = 1):
    auth_redirect = _login_required_redirect(request)
    if auth_redirect:
        return auth_redirect
    settings = get_settings()
    cleaned = q.strip()
    capped = max(1, min(limit, 100))
    page_num = max(1, min(int(page or 1), 5))
    local = db.search_videos(cleaned, capped) if cleaned else db.list_recent_videos(capped)

    # Searching only the local library answered "which videos do I already have that match"
    # rather than the question anyone actually asks. Results come from YouTube via
    # Invidious, with anything already in the library shown first because we know more
    # about it — its watch progress, whether the channel is subscribed, and so on.
    remote: list[dict[str, Any]] = []
    search_error = ""
    if cleaned:
        try:
            remote = discovery.search_youtube(cleaned, page=page_num)
        except Exception as exc:
            search_error = str(exc)
            logger.warning("youtube search failed for %r: %s", cleaned, exc)

    known = {str(video.get("youtube_video_id") or "") for video in local}
    fresh = [item for item in remote if item.get("youtube_video_id") not in known]
    if fresh:
        # Stored so the watch page, playlists and feedback all work on a search result the
        # same way they do on anything else — without this a result opened to a 404.
        try:
            db.store_search_results(fresh[:capped], cleaned)
        except Exception as exc:
            logger.warning("could not store search results for %r: %s", cleaned, exc)

    # Each source gets its own cap. The old `capped - len(local)` slice meant a full
    # library page left zero slots for YouTube, so search silently became library-only
    # as the library grew.
    local_shown = local[:capped]
    fresh_shown = fresh[:capped]
    combined = local_shown + fresh_shown
    rows = "".join(
        _media_row(video, settings, "In your library" if video.get("youtube_video_id") in known else "From YouTube")
        for video in combined
    )
    if not rows:
        rows = (
            f'<p class="empty-copy">Search is unavailable right now: {escape(search_error)}</p>'
            if search_error
            else '<p class="empty-copy">No matching videos.</p>'
        )
    heading = f'Results for "{escape(cleaned)}"' if cleaned else "Recent videos"
    # Page 1 shows every library match, so there is nothing more to load when the
    # query is empty (recent videos) or YouTube could not be reached.
    scrollable = bool(cleaned and not search_error)
    scroll_html = (
        '<div class="feed-loading" data-search-loading hidden><span></span><span>Loading more results</span></div>\n'
        '      <div class="feed-sentinel" data-search-sentinel aria-hidden="true"></div>'
        if scrollable
        else ""
    )
    content = f'''
      <section class="page-hero search-summary">
        <div>
          <p class="page-kicker">Search</p>
          <h1>{heading}</h1>
        </div>
      </section>
      <section class="result-list" data-search-list data-q="{escape(cleaned, quote=True)}"
               data-next-page="2" data-search-ok="{'1' if scrollable else '0'}">{rows}</section>
      {scroll_html}
    '''
    return _app_shell("Search", "search", content, search_value=cleaned, request=request)


def _timeline_day_label(value: Any) -> str:
    if not isinstance(value, datetime):
        return "Earlier"
    date = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    local = date.astimezone()
    today = datetime.now().astimezone().date()
    delta = (today - local.date()).days
    stamp = local.strftime("%d %b %Y")
    if delta <= 0:
        return f"Today - {stamp}"
    if delta == 1:
        return f"Yesterday - {stamp}"
    if delta < 7:
        return f"{local.strftime('%A')} - {stamp}"
    return stamp


def _subscription_timeline(videos: list[dict[str, Any]], settings: Any) -> str:
    groups: list[tuple[str, list[dict[str, Any]]]] = []
    for video in videos:
        label = _timeline_day_label(video.get("published_at"))
        if groups and groups[-1][0] == label:
            groups[-1][1].append(video)
        else:
            groups.append((label, [video]))
    if not groups:
        return '<p class="empty-copy">No videos from your subscriptions yet. Run a sync to pull them in.</p>'
    blocks = []
    for index, (label, items) in enumerate(groups):
        cards = "".join(_home_video_card(video, position, settings) for position, video in enumerate(items, start=1))
        group_class = "timeline-group" if index == 0 else "timeline-group past"
        blocks.append(
            f'<section class="{group_class}" data-filter-group>'
            f'<p class="timeline-date">{escape(label)}</p>'
            f'<div class="video-grid">{cards}</div></section>'
        )
    return f'<div class="timeline" data-section-kind="subscription">{"".join(blocks)}</div>'


def _takeout_pane_html(user_key: str = "local") -> str:
    """The Takeout importer, as a Settings pane.

    It lived on its own page reached from the sidebar, which put a one-time setup task
    permanently between Downloads and Channels. Settings is where one-time setup belongs.
    """
    existing = db.latest_takeout_import(user_key)
    summary = existing.get("summary") if existing and isinstance(existing.get("summary"), dict) else {}
    status_html = ""
    if existing:
        when = escape(_relative_time(existing.get("created_at")) or "recently")
        counts = db.takeout_subscription_counts(user_key)
        pending = counts.get("pending", 0)
        unavailable = counts.get("unavailable", 0)
        apply_button = (
            f'<button class="pill-action" type="button" data-takeout-apply-subs>Subscribe to {pending} channels</button>'
            if pending else ""
        )
        # Say why the numbers do not add up, rather than leaving it looking like a failure.
        unavailable_note = (
            f'<p class="dialog-note">{unavailable} channel{"" if unavailable == 1 else "s"} in your export no longer '
            f'exist on YouTube, so there is no feed to subscribe to. '
            f'<button class="link-action" type="button" data-takeout-retry-subs>Try them again</button></p>'
        ) if unavailable else ""
        status_html = f'''
          <div class="import-status" data-import-status>
            <div>
              <strong>Imported {when}</strong>
              <p>{int(summary.get("watched_videos") or 0):,} videos &middot; {int(summary.get("watched_channels") or 0):,} channels
                 &middot; {counts.get("total", 0):,} subscriptions</p>
            </div>
            <div class="import-status-actions">
              {apply_button}
              <button class="pill-action" type="button" data-feed-refresh>Refresh feed</button>
              <button class="pill-action danger" type="button" data-takeout-delete>Delete imported data</button>
            </div>
          </div>
          {unavailable_note}
          <p class="dialog-note import-note">{_icon("clock")}<span><strong>Your feed takes a while to catch up.</strong>
             Importing tells OpenFeed what you like, but it still has to go and fetch videos on those
             subjects &mdash; from the channels you watch, from searches on your top topics, and from
             what is trending. That runs in the background every sync, and new videos need their
             details fetched before topic matching can see them. Expect the feed to keep shifting
             toward your interests over the next few hours rather than changing the moment you
             upload. <button class="link-action" type="button" data-feed-refresh>Refresh now</button>
             runs a pass immediately.</span></p>
        '''
    return f'''
      <section class="page-hero">
        <div>
          <p class="page-kicker">Personalization</p>
          <h1>Import your YouTube history</h1>
          <p>OpenFeed starts out knowing nothing about you. Your Google Takeout export already
             contains years of what you watched and whose channels you watch most — importing it
             skips the cold start and makes recommendations useful immediately.</p>
        </div>
      </section>

      {status_html}

      <section class="import-layout">
        <ol class="import-steps">
          <li>
            <h3>Open Google Takeout</h3>
            <p>Go to <a href="https://takeout.google.com/" target="_blank" rel="noopener noreferrer">takeout.google.com</a>
               and sign in with the account you watch YouTube on.</p>
          </li>
          <li>
            <h3>Select YouTube only</h3>
            <p>Click <strong>Deselect all</strong>, scroll down, and tick just
               <strong>YouTube and YouTube Music</strong>. Selecting everything produces an
               archive many gigabytes larger with nothing extra that this import can use.</p>
          </li>
          <li>
            <h3>Choose JSON for history</h3>
            <p>Press <strong>Multiple formats</strong> and set <strong>History</strong> to
               <strong>JSON</strong>. This is the step people miss — Takeout defaults to HTML,
               which cannot be parsed reliably, and the import will tell you so if you get it wrong.</p>
          </li>
          <li>
            <h3>Narrow the content</h3>
            <p>Press <strong>All YouTube data included</strong> and keep only
               <strong>history</strong> and <strong>subscriptions</strong>. Leaving videos ticked
               means Takeout exports every video you have ever uploaded.</p>
          </li>
          <li>
            <h3>Export and wait</h3>
            <p>Choose <strong>Export once</strong>, <strong>.zip</strong>, and the largest size.
               Google emails you a link, usually within minutes but sometimes hours.</p>
          </li>
          <li>
            <h3>Upload it here</h3>
            <p>Drop the <code>.zip</code> below exactly as Google gave it to you — no need to unzip.</p>
          </li>
        </ol>

        <div class="import-panel">
          <div class="import-drop" data-import-drop tabindex="0" role="button" aria-label="Choose a Takeout archive">
            {_icon("download")}
            <strong>Drop your Takeout .zip here</strong>
            <small>or click to choose a file</small>
            <input type="file" accept=".zip,application/zip" data-import-file hidden>
          </div>
          <div class="import-progress" data-import-progress hidden>
            <div class="download-bar is-indeterminate"><span></span></div>
            <p class="dialog-note" data-import-state>Reading your archive&hellip;</p>
          </div>
          <div class="import-result" data-import-result hidden></div>
          <p class="dialog-note privacy-note">Your archive is parsed on this server and never
             leaves it. Only the counts that feed ranking are kept — which channels you watch and
             how often, and the IDs of videos you have already seen. The archive itself is not
             written to disk, and <strong>Delete imported data</strong> removes everything it added.</p>
        </div>
      </section>
    '''


@app.get("/import", response_class=HTMLResponse, tags=["pages"], summary="Redirects to Settings")
def import_page(request: Request):
    """Importing is one-time setup, so it lives in Settings now."""
    return RedirectResponse(url="/?settings=import", status_code=307)


@app.get("/subscriptions", response_class=HTMLResponse)
def subscriptions_page(request: Request, limit: int = 60):
    """Latest uploads from subscribed channels, newest first.

    Kept separate from /channels, which manages *which* channels you follow. Collapsing
    the two is what made the old "Subscriptions" entry feel like it went nowhere in
    particular — it opened a channel grid rather than the feed people expect.
    """
    auth_redirect = _login_required_redirect(request)
    if auth_redirect:
        return auth_redirect
    settings = get_settings()
    user_key = _user_key(request)
    preferences = db.get_feed_preferences(user_key)
    include_shorts = not preferences.get("exclude_shorts", False)
    page_size = max(1, min(limit, 200))
    videos = db.list_subscription_uploads(page_size, user_key, include_shorts)
    posts_html = _subscription_posts_shelf(user_key) if preferences.get("show_posts_in_feeds") else ""
    cards = "".join(_home_video_card(video, index, settings) for index, video in enumerate(videos))
    if not cards:
        cards = (
            '<p class="empty-copy">No uploads yet. Subscribe to channels and they '
            'will appear here after the next sync.</p>'
        )
    cards = posts_html + cards
    content = f'''
      <section class="page-hero">
        <div>
          <p class="page-kicker">Subscriptions</p>
          <h1>Latest from your subscriptions</h1>
          <p>Every upload from the channels you follow, newest first. <a href="/channels">Manage channels</a>.</p>
        </div>
      </section>
      <div class="video-grid" data-view-target>{cards}</div>
    '''
    return _app_shell("Subscriptions", "subscriptions", content, request=request)


@app.get("/channels", response_class=HTMLResponse)
def channels_page(request: Request, limit: int = 200, collection: str = "", view: str = "timeline", tab: str = ""):
    auth_redirect = _login_required_redirect(request)
    if auth_redirect:
        return auth_redirect
    if collection and _valid_uuid(collection):
        return RedirectResponse(url=f"/collection/{collection}", status_code=307)
    settings = get_settings()
    user_key = _user_key(request)
    subscriptions = db.list_subscriptions(max(1, min(limit, 500)))
    collections = db.list_subscription_collections(user_key)
    view_key = view if view in {"timeline", "channels"} else "timeline"
    collections_active = tab == "collections"

    timeline_videos = db.list_subscription_recommendations(60, user_key)
    timeline_html = _subscription_timeline(timeline_videos, settings)

    subscription_cards = "".join(_subscription_card(subscription) for subscription in subscriptions)
    if not subscription_cards:
        subscription_cards = '<p class="empty-copy">No subscriptions yet.</p>'

    collection_cards = "".join(_collection_card(item) for item in collections)
    if not collection_cards:
        collection_cards = (
            '<p class="empty-copy">No collections yet. Create one to group subscribed channels — '
            'a collection gets its own page with a combined feed.</p>'
        )
    uncollected = 0
    membership = db.list_collection_ids_for_subscriptions(user_key)
    for subscription in subscriptions:
        if not membership.get(str(subscription.get("id") or "")):
            uncollected += 1

    content = f'''
      <div class="page-tabs" role="tablist" aria-label="Subscription views">
        <button class="page-tab{'' if collections_active else ' active'}" type="button" data-page-tab="subscriptions" role="tab">Subscriptions <span class="count">{len(subscriptions)}</span></button>
        <button class="page-tab{' active' if collections_active else ''}" type="button" data-page-tab="collections" role="tab">Collections <span class="count">{len(collections)}</span></button>
      </div>

      <section data-tab-panel="subscriptions"{' hidden' if collections_active else ''}>
        <div class="toolbar">
          <a class="text-action{' active' if view_key == 'timeline' else ''}" href="/channels?view=timeline">{_icon("sort")}<span>View as: Timeline</span></a>
          <a class="text-action{' active' if view_key == 'channels' else ''}" href="/channels?view=channels">{_icon("collections")}<span>View as: Channels</span></a>
          <span class="toolbar-divider" aria-hidden="true"></span>
          <span class="toolbar-note">{_icon("calendar")}<span>{len(timeline_videos)} recent videos</span></span>
          <label class="toolbar-search">{_icon("search")}<input type="search" placeholder="Search subscriptions" data-filter-input aria-label="Filter subscriptions"></label>
        </div>
        {timeline_html if view_key == "timeline" else f'<div class="channel-grid">{subscription_cards}</div>'}
      </section>

      <section data-tab-panel="collections" id="collections"{'' if collections_active else ' hidden'}>
        <div class="toolbar">
          <button class="primary-action" type="button" data-new-collection>{_icon("plus")}<span>New collection</span></button>
          <span class="toolbar-divider" aria-hidden="true"></span>
          <span class="toolbar-note">{_icon("collections")}<span>{len(collections)} collection{"" if len(collections) == 1 else "s"}</span></span>
          <span class="toolbar-note">{_icon("subs")}<span>{uncollected} channel{"" if uncollected == 1 else "s"} not in a collection</span></span>
          <label class="toolbar-search">{_icon("search")}<input type="search" placeholder="Search collections" data-filter-input aria-label="Filter collections"></label>
        </div>
        <div class="collection-grid">{collection_cards}</div>
      </section>
    '''
    # The sidebar has separate entries for the two tabs on this screen, so the active
    # one has to follow the tab rather than always being "All channels".
    nav_key = "collections" if collections_active else "channels"
    page_title = "Collections" if collections_active else "Subscriptions"
    return _app_shell(page_title, nav_key, content, request=request)


# --- library sections ---------------------------------------------------------------
# Each of these renders exactly one thing and nothing else, so a nav item can point at a
# page that *is* that thing rather than a page that merely contains it.


def _history_section_html(user_key: str, settings: Any, limit: int = 120) -> str:
    history = db.list_history_videos(limit, user_key)
    groups: list[tuple[str, list[dict[str, Any]]]] = []
    for video in history:
        label = _timeline_day_label(video.get("latest_event_at") or video.get("published_at"))
        if groups and groups[-1][0] == label:
            groups[-1][1].append(video)
        else:
            groups.append((label, [video]))
    if not groups:
        return '<p class="empty-copy">No watch history recorded yet. Videos you open show up here.</p>'
    blocks = []
    for index, (label, items) in enumerate(groups):
        rows = "".join(_media_row(video, settings, "History") for video in items)
        group_class = "timeline-group" if index == 0 else "timeline-group past"
        blocks.append(f'<section class="{group_class}"><p class="timeline-date">{escape(label)}</p><div class="library-list">{rows}</div></section>')
    return f'<div class="timeline">{"".join(blocks)}</div>'


def _playlists_section_html(user_key: str, settings: Any, limit: int = 120) -> str:
    custom = db.list_playlists(user_key)
    cards = "".join(_playlist_card(playlist) for playlist in custom)
    if not cards:
        cards = '<p class="empty-copy">No playlists yet. Create one, then use \u201cSave to playlist\u201d on any video.</p>'
    feeds = db.list_playlist_subscriptions(limit)
    feed_section = ""
    if feeds:
        feed_section = f'''
          <section class="section-block">
            <div class="section-heading"><h2>Playlist feeds</h2></div>
            <div class="channel-grid">{"".join(_subscription_card(item) for item in feeds)}</div>
          </section>
        '''
    return f'<div class="playlist-grid">{cards}</div>{feed_section}'


def _liked_section_html(user_key: str, settings: Any, limit: int = 120) -> str:
    liked = db.list_liked_videos(limit, user_key)
    grid = "".join(_home_video_card(video, index, settings) for index, video in enumerate(liked, start=1))
    if not grid:
        return '<p class="empty-copy">No liked videos yet. Like a video to save it here.</p>'
    return f'<div class="video-grid">{grid}</div>'


def _downloads_section_html(user_key: str, settings: Any) -> str:
    rows = db.list_downloads(user_key, "", 500)
    grid = "".join(_download_card(row, settings) for row in rows) or (
        '<p class="empty-copy">No downloads yet. Pick <strong>Keep on server</strong> in the download '
        'dialog and the video will appear here, playable without touching YouTube.</p>'
    )
    total = _bytes_text(sum(int(row.get("size_bytes") or 0) for row in rows))
    return f'''
      <div class="toolbar">
        <span class="toolbar-note">{_icon("download")}<span data-downloads-summary>{len(rows)} download{"" if len(rows) == 1 else "s"}{f" &middot; {total}" if total else ""}</span></span>
        <label class="toolbar-search">{_icon("search")}<input type="search" placeholder="Search downloads" data-downloads-search aria-label="Search downloads"></label>
      </div>
      <div class="video-grid" data-downloads-grid>{grid}</div>
    '''


def _library_page_shell(request: Request, title: str, kicker: str, blurb: str, body: str, nav_key: str, actions: str = ""):
    content = f'''
      <section class="page-hero">
        <div>
          <p class="page-kicker">{escape(kicker)}</p>
          <h1>{escape(title)}</h1>
          <p>{escape(blurb)}</p>
        </div>
        {f'<div class="page-actions">{actions}</div>' if actions else ""}
      </section>
      {body}
    '''
    return _app_shell(title, nav_key, content, request=request)


@app.get("/history", response_class=HTMLResponse, tags=["pages"], summary="Watch history")
def history_page(request: Request, limit: int = 120):
    auth_redirect = _login_required_redirect(request)
    if auth_redirect:
        return auth_redirect
    user_key = _user_key(request)
    return _library_page_shell(
        request, "History", "You", "Everything you have watched, newest first.",
        _history_section_html(user_key, get_settings(), max(1, min(limit, 400))), "history",
    )


@app.get("/playlists", response_class=HTMLResponse, tags=["pages"], summary="Playlists")
def playlists_page(request: Request, limit: int = 120):
    auth_redirect = _login_required_redirect(request)
    if auth_redirect:
        return auth_redirect
    user_key = _user_key(request)
    return _library_page_shell(
        request, "Playlists", "You", "Collections of videos you have saved.",
        _playlists_section_html(user_key, get_settings(), max(1, min(limit, 400))), "playlists",
        actions=f'<button class="primary-action" type="button" data-new-playlist>{_icon("plus")}<span>New playlist</span></button>',
    )


@app.get("/liked", response_class=HTMLResponse, tags=["pages"], summary="Liked videos")
def liked_page(request: Request, limit: int = 120):
    auth_redirect = _login_required_redirect(request)
    if auth_redirect:
        return auth_redirect
    user_key = _user_key(request)
    return _library_page_shell(
        request, "Liked videos", "You", "Videos you liked here.",
        _liked_section_html(user_key, get_settings(), max(1, min(limit, 400))), "liked",
    )


@app.get("/downloads", response_class=HTMLResponse, tags=["pages"], summary="Downloaded videos")
def downloads_page(request: Request):
    auth_redirect = _login_required_redirect(request)
    if auth_redirect:
        return auth_redirect
    user_key = _user_key(request)
    return _library_page_shell(
        request, "Downloads", "You", "Videos kept on the server. These play without touching YouTube.",
        _downloads_section_html(user_key, get_settings()), "downloads",
    )


@app.get("/collections", response_class=HTMLResponse, tags=["pages"], summary="Redirects to the Collections tab")
def collections_page():
    """Collections live on the Subscriptions screen, which already has a tab for them.

    A standalone page here meant two screens rendering the same collections. This keeps
    the tidy URL and sends it to the one real screen with the right tab open.
    """
    return RedirectResponse(url="/channels?tab=collections", status_code=307)


@app.get("/library", response_class=HTMLResponse, tags=["pages"], summary="Redirects into the You pages")
def library_page(request: Request, tab: str = ""):
    """The four things this page held are now pages in their own right.

    Kept as a redirect, and it honours the old tab parameter so existing links land on
    the right content instead of dumping everyone in the same place.
    """
    target = {
        "history": "/history",
        "playlists": "/playlists",
        "liked": "/liked",
        "downloads": "/downloads",
    }.get(tab, "/history")
    return RedirectResponse(url=target, status_code=307)


@app.get("/playlist/{playlist_id}", response_class=HTMLResponse)
def playlist_page(request: Request, playlist_id: str):
    auth_redirect = _login_required_redirect(request)
    if auth_redirect:
        return auth_redirect
    settings = get_settings()
    user_key = _user_key(request)
    if not _valid_uuid(playlist_id):
        raise HTTPException(status_code=404, detail="Playlist not found")
    playlist = db.get_playlist(playlist_id, user_key)
    if not playlist:
        raise HTTPException(status_code=404, detail="Playlist not found")
    videos = db.list_playlist_videos(playlist_id, 300, user_key)
    name_text = str(playlist.get("name") or "Untitled playlist")
    name = escape(name_text)
    playlist_id_attr = escape(str(playlist.get("id") or ""), quote=True)
    description_text = str(playlist.get("description") or "")
    count = len(videos)
    updated = _relative_time(playlist.get("latest_video_at") or playlist.get("updated_at"))
    meta = f"{count} video{'' if count == 1 else 's'}"
    if updated:
        meta += f" · updated {updated}"
    cover = escape(_thumbnail_for(videos[0])) if videos else ""
    cover_html = (
        f'<img src="{cover}" alt="" loading="lazy" onerror="this.remove()">'
        if cover else f'<span class="playlist-cover-empty">{_icon("playlist")}</span>'
    )
    play_all = (
        f'<a class="primary-action" href="/watch/{escape(str(videos[0].get("youtube_video_id") or ""))}">{_icon("play")}<span>Play all</span></a>'
        if videos else ""
    )
    rows = []
    for index, video in enumerate(videos, start=1):
        video_id_raw = str(video.get("youtube_video_id") or "")
        video_id = escape(video_id_raw, quote=True)
        remove = (
            f'<button class="playlist-remove" type="button" data-remove-playlist-item="{video_id}" '
            f'data-playlist-id="{playlist_id_attr}" aria-label="Remove from playlist">{_icon("x")}</button>'
        )
        rows.append(
            _media_row(
                video,
                settings,
                index=index,
                playlist_row_id=video_id_raw or None,
                trailing=remove,
            )
        )
    items_html = "".join(rows) or '<p class="empty-copy">This playlist is empty. Use “Save to playlist” from any video menu.</p>'
    description_html = f'<p class="playlist-description">{escape(description_text)}</p>' if description_text else ""

    content = f'''
      <div class="playlist-detail">
        <aside class="playlist-panel">
          <span class="playlist-cover">{cover_html}</span>
          <h1>{name}</h1>
          <p class="playlist-meta">{escape(meta)}</p>
          {description_html}
          <div class="playlist-panel-actions">
            {play_all}
            <button class="text-action" type="button" data-edit-playlist="{playlist_id_attr}" data-playlist-name="{escape(name_text, quote=True)}" data-playlist-description="{escape(description_text, quote=True)}">{_icon("pencil")}<span>Rename</span></button>
            <button class="text-action" type="button" data-delete-playlist="{playlist_id_attr}" data-redirect="/playlists">{_icon("trash")}<span>Delete</span></button>
          </div>
        </aside>
        <section class="playlist-items">{items_html}</section>
      </div>
    '''
    return _app_shell(name_text, "library", content, request=request)


@app.get("/channel/{youtube_channel_id}/posts", response_class=HTMLResponse, tags=["pages"], summary="Channel community posts")
def channel_posts_page(request: Request, youtube_channel_id: str, continuation: str = ""):
    """The Posts tab. Available whether or not posts are enabled in the feeds."""
    auth_redirect = _login_required_redirect(request)
    if auth_redirect:
        return auth_redirect
    from . import posts as posts_module

    channel = db.get_channel_by_youtube_id(youtube_channel_id)
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    user_key = _user_key(request)
    try:
        page = posts_module.channel_posts(youtube_channel_id, continuation)
        error = ""
    except posts_module.PostsError as exc:
        page, error = {"posts": [], "continuation": ""}, str(exc)

    items = page.get("posts") or []
    votes = {post["post_id"]: db.get_poll_vote(post["post_id"], user_key) for post in items}
    cards = "".join(_post_card(post, votes.get(post["post_id"])) for post in items)
    if not cards:
        cards = (
            f'<p class="empty-copy">{escape(error) if error else "This channel has no community posts, or has them turned off."}</p>'
        )
    more = ""
    if page.get("continuation"):
        more = (
            f'<div class="post-more"><a class="pill-action" href="/channel/{escape(youtube_channel_id, quote=True)}'
            f'/posts?continuation={urllib.parse.quote(page["continuation"])}">Load older posts</a></div>'
        )
    title_text = str(channel.get("title") or "Channel")
    content = f'''
      <section class="page-hero">
        <div>
          <p class="page-kicker">{escape(title_text)}</p>
          <h1>Posts</h1>
          <p>Text, images and polls this channel shared outside its uploads.</p>
        </div>
        <div class="page-actions"><a class="pill-action" href="/channel/{escape(youtube_channel_id, quote=True)}">{_icon("chevron-left")}<span>Videos</span></a></div>
      </section>
      <div class="post-list">{cards}</div>
      {more}
    '''
    return _app_shell(f"{title_text} posts", "channels", content, request=request)


@app.get("/channel/{youtube_channel_id}/posts/{post_id}", response_class=HTMLResponse, tags=["pages"], summary="A single post")
def post_detail_page(request: Request, youtube_channel_id: str, post_id: str):
    auth_redirect = _login_required_redirect(request)
    if auth_redirect:
        return auth_redirect
    from . import posts as posts_module

    user_key = _user_key(request)
    post = posts_module.single_post(post_id, youtube_channel_id)
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    replies = posts_module.post_comments(post_id, youtube_channel_id)
    reply_html = "".join(
        f'<article class="comment"><div class="comment-head">'
        f'<span class="comment-author">{escape(str(item.get("author") or ""))}</span>'
        f'<span class="comment-time">{escape(str(item.get("publishedText") or ""))}</span></div>'
        f'<p class="comment-body">{escape(str(item.get("content") or ""))}</p></article>'
        for item in (replies.get("comments") or [])[:50]
        if isinstance(item, dict)
    )
    content = f'''
      <section class="page-hero">
        <div>
          <p class="page-kicker">Post</p>
          <h1>{escape(str(post.get("author") or "Post"))}</h1>
          <p>{escape(post.get("published_text") or "")}</p>
        </div>
        <div class="page-actions"><a class="pill-action" href="/channel/{escape(youtube_channel_id, quote=True)}/posts">{_icon("chevron-left")}<span>All posts</span></a></div>
      </section>
      <div class="post-list">{_post_card(post, db.get_poll_vote(post_id, user_key))}</div>
      {f'<section class="comments"><div class="comments-head"><h2>Replies</h2></div>{reply_html}</section>' if reply_html else ""}
    '''
    return _app_shell("Post", "channels", content, request=request)


@app.post("/api/posts/{post_id}/vote", tags=["playback"], summary="Record a simulated poll answer")
def api_post_vote(request: Request, post_id: str, payload: PollVoteRequest) -> dict[str, Any]:
    """Store which option the user picked.

    Local only. YouTube publishes no per-option counts and accepts no votes without an
    authenticated account, so there is nowhere to send this even if we wanted to.
    """
    auth_redirect = _login_required_redirect(request)
    if auth_redirect:
        raise HTTPException(status_code=401, detail="Login required")
    from . import posts as posts_module

    if not posts_module.valid_post_id(post_id):
        raise HTTPException(status_code=400, detail="Invalid post id")
    user_key = _user_key(request)
    if payload.choice_index is None or payload.choice_index < 0:
        return {"ok": True, "cleared": db.clear_poll_vote(post_id, user_key), "choice_index": None, "submitted_to_youtube": False}
    choice = db.set_poll_vote(post_id, payload.choice_index, user_key)
    return {"ok": True, "choice_index": choice, "submitted_to_youtube": False}


_URL_IN_TEXT = re.compile(r"https?://[^\s<>\"'\)]+")


def _link_label(url: str) -> str:
    """The bare domain and path, the way YouTube prints a link."""
    return re.sub(r"^https?://(www\.)?", "", url).rstrip("/")[:48]


def _description_links(description: str) -> list[tuple[str, str]]:
    """Named links pulled out of a channel description.

    Invidious exposes no link fields at all, so a creator's own description is the only
    honest source. Creators almost always write them as "IG: https://…", which is where
    the name comes from; when there is no such prefix the domain stands in for it.
    """
    if not description:
        return []
    links: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line in description.splitlines():
        for match in _URL_IN_TEXT.finditer(line):
            url = match.group(0).rstrip(".,);:")
            if url in seen:
                continue
            seen.add(url)
            # Text before the URL on the same line, minus the punctuation creators use to
            # attach it. Anything long is prose rather than a label, so drop it.
            prefix = line[: match.start()].strip().strip("-–—:•|»>*").strip()
            label = prefix if 0 < len(prefix) <= 32 else _link_label(url)
            links.append((label, url))
            if len(links) >= 12:
                return links
    return links


def _channel_about_dialog(
    title_text: str, description: str, links: list[tuple[str, str]], youtube_channel_id: str,
    handle: str, subscriber_count: str, video_count: int, latest: str, subscription_link: str,
) -> str:
    """The "..more" popup, the way YouTube does it.

    Expanding the description in place pushed the page around and left no way to close it
    again, so the full text lives here instead.
    """
    if not description and not links:
        return ""
    sections = []
    if description:
        sections.append(
            f'<section class="ch-about-section"><h3>Description</h3>'
            f'<div class="ch-about-text">{_rich_text(description[:6000])}</div></section>'
        )
    if links:
        rows = "".join(
            f'<li>{_icon("external")}<div><span class="ch-link-name">{escape(label)}</span>'
            f'<a href="{escape(url, quote=True)}" target="_blank" rel="noopener noreferrer nofollow">'
            f'{escape(_link_label(url))}</a></div></li>'
            for label, url in links
        )
        sections.append(f'<section class="ch-about-section"><h3>Links</h3><ul class="ch-link-list">{rows}</ul></section>')

    # Only what we actually know. This Invidious build reports `joined` and `totalViews`
    # as 0 for every channel, so a "Joined" or "Views" row here would be a fabrication —
    # and the video count is what OpenFeed has stored, not YouTube's total, so it says so.
    info_rows = []
    channel_url = subscription_link or (
        f"https://www.youtube.com/@{handle.lstrip('@')}" if handle
        else f"https://www.youtube.com/channel/{youtube_channel_id}"
    )
    info_rows.append(
        f'<li>{_icon("subs")}<a href="{escape(channel_url, quote=True)}" target="_blank" '
        f'rel="noopener noreferrer nofollow">{escape(_link_label(channel_url))}</a></li>'
    )
    if subscriber_count:
        info_rows.append(f'<li>{_icon("user")}<span>{escape(subscriber_count)} subscribers</span></li>')
    info_rows.append(f'<li>{_icon("play")}<span>{video_count} videos in your library</span></li>')
    if latest:
        info_rows.append(f'<li>{_icon("clock")}<span>Latest upload {escape(latest)}</span></li>')
    sections.append(f'<section class="ch-about-section"><h3>More info</h3><ul class="ch-info-list">{"".join(info_rows)}</ul></section>')

    return f'''
      <dialog class="feed-dialog ch-about-dialog" data-ch-about-dialog aria-labelledby="ch-about-title">
        <div class="fc-head">
          <h2 id="ch-about-title">{escape(title_text)}</h2>
          <button class="icon-button" type="button" data-close-dialog aria-label="Close">{_icon("x")}</button>
        </div>
        <div class="fc-body">{"".join(sections)}</div>
      </dialog>
    '''


@app.get("/channel/{youtube_channel_id}", response_class=HTMLResponse)
def channel_page(request: Request, youtube_channel_id: str, limit: int = 80, sort: str = "latest", filter: str = "", q: str = ""):
    auth_redirect = _login_required_redirect(request)
    if auth_redirect:
        return auth_redirect
    channel = db.get_channel_by_youtube_id(youtube_channel_id)
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")

    settings = get_settings()
    sort_key = sort if sort in {"latest", "popular", "oldest"} else "latest"
    bounded_limit = max(1, min(limit, 500))
    kind = filter if filter in {"shorts", "playlists"} else ""
    query = (q or "").strip()
    # Search the whole stored channel in SQL, not just the first page in Python.
    # The old in-memory filter made "no results" mean "no results in the first 80".
    fetch_limit = bounded_limit * 3 if kind == "shorts" and not query else bounded_limit
    videos = db.list_channel_videos(youtube_channel_id, fetch_limit, sort_key, 0, query)
    if not query and len(videos) < bounded_limit and kind != "playlists":
        # First paint was capped at whatever RSS/sync had stored (~15 videos).
        # Top up from the channel itself so the page reflects the channel, not the cache.
        try:
            if _ingest_channel_uploads(youtube_channel_id, bounded_limit, sort_key, max_pages=3):
                videos = db.list_channel_videos(youtube_channel_id, fetch_limit, sort_key, 0, query)
        except Exception as exc:
            logger.info("channel backfill failed for %s: %s", youtube_channel_id, exc)
    if kind == "shorts":
        videos = [v for v in videos if db.is_short_video(v)][:bounded_limit]
    elif kind == "playlists":
        videos = []
    else:
        videos = videos[:bounded_limit]
    title_text = str(channel.get("title") or "Unknown channel")
    title = escape(title_text)
    video_count = int(channel.get("video_count") or len(videos))
    latest = _relative_time(channel.get("latest_video_at"))
    initials = escape(_avatar_initials(title_text))
    hue = _avatar_hue(title_text)
    subscription_link = escape(channel.get("subscription_html_url") or "")
    feed_url_raw = str(channel.get("feed_url") or _youtube_feed_url(youtube_channel_id))
    subscribed = bool(channel.get("subscription_id"))
    notify_level = db.get_channel_notification(youtube_channel_id, _user_key(request)) if subscribed else "all"
    subscribe_button = _subscribe_control(youtube_channel_id, feed_url_raw, title_text, subscribed, notify_level)
    banner_url = str(channel.get("banner_url") or "").strip()
    if banner_url:
        banner_html = (
            f'<section class="channel-banner has-image" aria-hidden="true">'
            f'<img src="{escape(banner_url, quote=True)}" alt="" decoding="async" '
            f'onerror="this.closest(\'.channel-banner\').classList.remove(\'has-image\'); this.remove()"></section>'
        )
    else:
        banner_html = (
            f'<section class="channel-banner" style="--avatar-hue:{hue}" aria-hidden="true">{escape(title_text[:28])}</section>'
        )
    subscription_id_raw = str(channel.get("subscription_id") or "")
    collection_button = ""
    if subscription_id_raw:
        collection_button = (
            f'<button class="text-action" type="button" data-channel-collections="{escape(subscription_id_raw, quote=True)}" '
            f'data-channel-title="{escape(title_text, quote=True)}">{_icon("collections")}<span>Add to collection</span></button>'
        )
    video_grid = "".join(_home_video_card(video, index, settings) for index, video in enumerate(videos, start=1))
    if not video_grid:
        video_grid = (
            '<p class="empty-copy">Playlists are not imported yet.</p>' if kind == "playlists"
            else f'<p class="empty-copy">No results for &ldquo;{escape(query)}&rdquo; in this channel.</p>' if query
            else '<p class="empty-copy">No Shorts from this channel yet.</p>' if kind == "shorts"
            else '<p class="empty-copy">No videos imported for this channel yet.</p>'
        )
    # Sorting is a filter on one tab, not a tab of its own — the old page presented
    # Latest/Popular/Oldest as top-level tabs alongside nothing else, which made the page
    # look like it had three sections when it has one.
    filter_html = "".join(
        f'<a class="ch-chip{" active" if sort_key == key else ""}" '
        f'href="/channel/{escape(youtube_channel_id)}?sort={key}&limit={bounded_limit}"'
        f'{" aria-current=\"page\"" if sort_key == key else ""}>{label}</a>'
        for key, label in (("latest", "Latest"), ("popular", "Popular"), ("oldest", "Oldest"))
    )

    subscriber_count = _compact_count(channel.get("subscriber_count"))
    handle = str(channel.get("handle") or "").strip()
    verified = _icon("verified").replace('class="icon"', 'class="icon verified"') if channel.get("verified") else ""
    identity_parts = []
    if handle:
        identity_parts.append(f'<span class="ch-handle">@{escape(handle.lstrip("@"))}</span>')
    if subscriber_count:
        identity_parts.append(f"{escape(subscriber_count)} subscribers")
    identity_parts.append(f"{video_count} videos here")
    if latest:
        identity_parts.append(f"latest {escape(latest)}")
    identity = ' <span class="ch-dot">&middot;</span> '.join(identity_parts)

    description = str(channel.get("description") or "").strip()
    # YouTube keeps the inline copy to one clamped line ending in "...more", and puts the
    # whole thing behind a dialog. Expanding in place is what made the avatar jump: the
    # identity row is a grid, so growing the copy column re-centres the avatar beside it.
    first_line = description.split("\n", 1)[0].strip() if description else ""
    description_html = (
        f'<div class="ch-about">'
        f'<p class="ch-about-short">{_rich_text(first_line[:220])}</p>'
        f'<button class="ch-about-more" type="button" data-ch-about-open>&hellip;more</button>'
        f'</div>'
    ) if description else ""

    # YouTube shows a dedicated links row. Invidious exposes no link fields, so the only
    # honest source is the URLs a creator wrote into their own description.
    found_links = _description_links(description)
    links_html = ""
    if found_links:
        first_label, first_url = found_links[0]
        extra = f'<span class="ch-dot">and {len(found_links) - 1} more</span>' if len(found_links) > 1 else ""
        links_html = (
            f'<p class="ch-links">{_icon("external")}'
            f'<a href="{escape(first_url, quote=True)}" target="_blank" rel="noopener noreferrer nofollow">'
            f'{escape(_link_label(first_url))}</a>{extra}</p>'
        )

    about_dialog = _channel_about_dialog(
        title_text, description, found_links, youtube_channel_id, handle,
        subscriber_count, video_count, latest, subscription_link,
    )

    available = {str(tab).lower() for tab in (channel.get("available_tabs") or [])}
    tabs = [("videos", "Videos", f"/channel/{escape(youtube_channel_id)}")]
    if not available or "shorts" in available:
        tabs.append(("shorts", "Shorts", f"/channel/{escape(youtube_channel_id)}?filter=shorts"))
    if not available or "playlists" in available:
        tabs.append(("playlists", "Playlists", f"/channel/{escape(youtube_channel_id)}?filter=playlists"))
    tabs.append(("posts", "Posts", f"/channel/{escape(youtube_channel_id)}/posts"))
    active_tab = "shorts" if kind == "shorts" else "playlists" if kind == "playlists" else "videos"
    tab_html = "".join(
        f'<a class="ch-tab{" active" if key == active_tab else ""}" href="{href}"'
        f'{" aria-current=\"page\"" if key == active_tab else ""}>{label}</a>'
        for key, label, href in tabs
    )

    overflow_rows = []
    if subscription_id_raw:
        overflow_rows.append(_menu_row(
            "collections", "Add to collection",
            attrs=f' data-channel-collections="{escape(subscription_id_raw, quote=True)}" data-channel-title="{escape(title_text, quote=True)}"'))
    if subscription_link:
        overflow_rows.append(_menu_row("external", "Open on YouTube", href=subscription_link))
    overflow = f'''
      <details class="ch-menu yt-menu">
        <summary class="pill-action pill-icon" aria-label="More channel actions">{_icon("more")}</summary>
        <div class="menu-panel" role="menu">{"".join(overflow_rows)}</div>
      </details>
    '''

    content = f'''
      <div class="ch-wrap">
      {banner_html}
      <section class="ch-identity">
        {_avatar_html(title_text, channel.get("avatar_url"), css_class="avatar ch-avatar")}
        <div class="ch-identity-copy">
          <h1>{title}{verified}</h1>
          <p class="ch-meta">{identity}</p>
          {description_html}
          {links_html}
          <div class="ch-actions">{subscribe_button}{overflow}</div>
        </div>
      </section>
      {about_dialog}
      <nav class="ch-tabs" aria-label="Channel sections">
        <div class="ch-tab-row">{tab_html}</div>
        <form class="ch-search{" is-open" if query else ""}" method="get" data-ch-search
              action="/channel/{escape(youtube_channel_id)}" role="search">
          <button class="ch-search-toggle" type="button" data-ch-search-toggle aria-label="Search this channel"
                  aria-expanded="{"true" if query else "false"}">{_icon("search")}</button>
          <input type="search" name="q" value="{escape(query, quote=True)}" placeholder="Search"
                 aria-label="Search this channel" data-ch-search-input>
        </form>
      </nav>
      {f'<div class="ch-filters">{filter_html}</div>' if active_tab == "videos" and not query else ""}
      <section class="video-grid" data-channel-grid data-channel-id="{escape(youtube_channel_id, quote=True)}"
               data-sort="{sort_key}" data-kind="{kind or "videos"}" data-q="{escape(query, quote=True)}" data-next-offset="{len(videos)}">{video_grid}</section>
      <div class="feed-loading" data-channel-loading hidden><span></span><span>Loading more videos</span></div>
      <div class="feed-sentinel" data-channel-sentinel aria-hidden="true"></div>
      </div>
    '''
    return _app_shell(title_text, "channels", content, main_class="channel-main", request=request)


@app.get("/api/videos")
def api_videos(limit: int = 50) -> dict[str, Any]:
    return {"items": _json_ready(db.list_recent_videos(max(1, min(limit, 200))))}


@app.get("/api/search")
def api_search(q: str = "", limit: int = 50, source: str = "all", page: int = 1) -> dict[str, Any]:
    """Search YouTube and the local library.

    `source=library` restricts it to what is already stored, which is what this endpoint
    used to do unconditionally. `page` pages the YouTube side (1-5); the library side is
    always the top matches. Each side gets its own `limit` so a full library page can
    no longer starve YouTube results to zero.
    """
    capped = max(1, min(limit, 200))
    page_num = max(1, min(int(page or 1), 5))
    local = db.search_videos(q, capped)
    if source == "library" or not q.strip():
        return {"items": _json_ready(local), "source": "library"}

    known = {str(video.get("youtube_video_id") or "") for video in local}
    remote: list[dict[str, Any]] = []
    error = ""
    try:
        remote = [item for item in discovery.search_youtube(q, page=page_num) if item.get("youtube_video_id") not in known]
        if remote:
            db.store_search_results(remote[:capped], q)
    except Exception as exc:
        error = str(exc)
        logger.warning("youtube search failed for %r: %s", q, exc)
    local_shown = local[:capped]
    fresh_shown = remote[:capped]
    return {
        "items": _json_ready(local_shown + fresh_shown),
        "library_count": len(local_shown),
        "youtube_count": len(fresh_shown),
        "page": page_num,
        "source": "library" if error else "all",
        "detail": error,
    }


@app.get("/api/search/html", tags=["search"], summary="Search results as rendered rows")
def api_search_html(request: Request, q: str = "", limit: int = 40, page: int = 2) -> dict[str, Any]:
    """The next pages of a search, already rendered.

    Returns markup for the same reason the channel feed does: the row is rendered
    server-side, and a second implementation of it in JavaScript would drift from
    this one.

    Page 1 is the search page itself, which shows every library match. Deeper
    pages are YouTube-only (`page` pages the YouTube side, 1-5), with anything
    already stored — including the page-1 YouTube rows, which are stored on
    sight — left out so scrolling never repeats a video.
    """
    settings = get_settings()
    cleaned = q.strip()
    capped = max(1, min(limit, 100))
    page_num = max(1, min(int(page or 1), 5))
    if not cleaned:
        return {"ok": True, "html": "", "count": 0, "next_page": page_num, "has_more": False}
    local = db.search_videos(cleaned, capped) if page_num == 1 else []
    try:
        remote = discovery.search_youtube(cleaned, page=page_num)
    except Exception as exc:
        logger.warning("youtube search failed for %r (page %d): %s", cleaned, page_num, exc)
        return {"ok": True, "html": "", "count": 0, "next_page": page_num, "has_more": False}

    if page_num == 1:
        # Exactly what the search page shows: the library matches first, then
        # YouTube with those left out.
        known = {str(video.get("youtube_video_id") or "") for video in local}
    else:
        # Page 1 already showed the top library matches; everything stored —
        # library videos and the stored page-1 YouTube rows — stays off.
        known = db.video_ids_in_library([item.get("youtube_video_id") for item in remote])
    fresh = [item for item in remote if item.get("youtube_video_id") not in known]
    if fresh:
        try:
            db.store_search_results(fresh[:capped], cleaned)
        except Exception as exc:
            logger.warning("could not store search results for %r: %s", cleaned, exc)

    shown = (local[:capped] if page_num == 1 else []) + fresh[:capped]
    html = "".join(
        _media_row(video, settings, "In your library" if video.get("youtube_video_id") in known else "From YouTube")
        for video in shown
    )
    return {
        "ok": True,
        "html": html,
        "count": len(shown),
        "next_page": page_num + 1,
        "has_more": page_num < 5 and bool(fresh),
    }


@app.get("/api/recommendations")
def api_recommendations(request: Request, limit: int = 50, offset: int = 0, filter: str = "all") -> dict[str, Any]:
    bounded_limit = max(1, min(limit, 200))
    bounded_offset = max(0, offset)
    return {"items": _json_ready(db.list_recommended_videos(bounded_limit, bounded_offset, filter, _user_key(request)))}


@app.get("/api/home/sections")
def api_home_sections(request: Request, page: int = 0, filter: str = "all") -> dict[str, Any]:
    settings = get_settings()
    html = _home_feed_sections_html(max(0, page), filter, settings, _user_key(request))
    return {"ok": True, "html": html, "next_page": max(0, page) + 1, "has_more": bool(html.strip())}


def _ingest_channel_uploads(youtube_channel_id: str, needed: int, sort_key: str = "latest", max_pages: int = 6) -> int:
    """Pull further pages of a channel's uploads into the library.

    Paged rather than fetched all at once, and stopped as soon as a page yields nothing
    new, so scrolling a channel costs one upstream request per page instead of hammering
    Invidious for a back catalogue that is mostly already stored.
    """
    sort_map = {"latest": "newest", "oldest": "oldest", "popular": "popular"}
    upstream_sort = sort_map.get(sort_key, "newest")
    stored = 0
    for page in range(1, max(1, max_pages) + 1):
        items = discovery.fetch_channel_uploads(youtube_channel_id, page, sort_by=upstream_sort)
        if not items:
            break
        added = sum(1 for item in items if db.upsert_discovered_video(item, None, "channel", "channel backfill"))
        stored += added
        # A page that was entirely already-known means the library has caught up.
        if not added or stored >= needed:
            break
    return stored


@app.get("/api/channels/{youtube_channel_id}/videos/html", tags=["subscriptions"], summary="Channel videos as rendered cards")
def api_channel_videos_html(request: Request, youtube_channel_id: str, offset: int = 0, limit: int = 24, sort: str = "latest", kind: str = "videos", q: str = "") -> dict[str, Any]:
    """The next page of a channel's videos, already rendered.

    Returns markup for the same reason the home feed does: the card is a substantial piece
    of HTML with a menu, progress rail and reaction state, and a second implementation of
    it in JavaScript would drift from this one.

    When the local library runs out, this reaches for more from the channel itself so a
    channel page is not limited to whatever happened to be synced.

    `kind` and `q` mirror the tab the grid was rendered from. Without them, scrolling a
    Shorts tab or a channel search appended Latest videos under it -- and scrolling the
    Playlists tab (which has no video grid at all) did the same.
    """
    settings = get_settings()
    sort_key = sort if sort in {"latest", "popular", "oldest"} else "latest"
    kind_key = kind if kind in {"shorts", "playlists"} else "videos"
    search = (q or "").strip()
    start = max(0, offset)
    size = max(1, min(limit, 60))
    if kind_key == "playlists":
        # Playlists live on their own tab with no video grid to extend; the sentinel
        # must not pull videos into it.
        return {"ok": True, "html": "", "count": 0, "fetched_remote": 0, "next_offset": start, "has_more": False}
    shorts_tab = kind_key == "shorts" and not search
    # A Shorts window scans past long-form videos, so it reads a wider window and
    # consumes the whole scan: advancing by the window (not the filtered count)
    # means no gaps and no duplicates at the boundaries.
    window = size * 3 if shorts_tab else size
    videos = db.list_channel_videos(youtube_channel_id, window, sort_key, start, search)

    fetched = 0
    if len(videos) < window:
        # Nothing left locally. Pull the channel's uploads from Invidious, store them, and
        # re-read so the page continues seamlessly instead of simply stopping.
        # Applies to every sort now — popular/oldest were previously local-only, which
        # made those tabs look empty on channels with a thin local cache.
        try:
            fetched = _ingest_channel_uploads(youtube_channel_id, start + window, sort_key)
        except Exception as exc:
            logger.info("channel backfill failed for %s: %s", youtube_channel_id, exc)
        if fetched:
            videos = db.list_channel_videos(youtube_channel_id, window, sort_key, start, search)

    if shorts_tab:
        scanned = len(videos)
        videos = [video for video in videos if db.is_short_video(video)][:size]
        next_offset = start + window if scanned >= window else start + scanned
        has_more = scanned >= window or fetched > 0
    else:
        next_offset = start + len(videos)
        has_more = len(videos) >= size

    html = "".join(_home_video_card(video, index, settings) for index, video in enumerate(videos))
    return {
        "ok": True,
        "html": html,
        "count": len(videos),
        "fetched_remote": fetched,
        "next_offset": next_offset,
        "has_more": has_more,
    }


@app.get("/api/recommendations/evaluate", tags=["recommendations"], summary="Offline ranker evaluation")
def api_evaluate_recommendations(request: Request, holdout: int = 20, depth: int = 100) -> dict[str, Any]:
    """Quality and coverage metrics for the ranker.

    Quality is measured by holding out the most recently watched videos and asking how
    many the ranker resurfaces. Coverage reports what the feed is made of, because a
    ranker can score well while showing four creators over and over.
    """
    auth_redirect = _login_required_redirect(request)
    if auth_redirect:
        raise HTTPException(status_code=401, detail="Login required")
    from . import evaluation

    return evaluation.full_report(_user_key(request), max(5, min(holdout, 100)), max(20, min(depth, 300)))


@app.post("/api/recommendations/rebuild-vectors", tags=["recommendations"], summary="Recompute topic vectors")
def api_rebuild_vectors(request: Request, limit: int = 500, force: bool = False) -> dict[str, Any]:
    auth_redirect = _login_required_redirect(request)
    if auth_redirect:
        raise HTTPException(status_code=401, detail="Login required")
    from . import topics

    return topics.rebuild_vectors(max(1, min(limit, 2000)), force)


@app.get("/api/takeout/subscriptions", tags=["recommendations"], summary="Imported subscriptions")
def api_takeout_subscriptions(request: Request, limit: int = 500) -> dict[str, Any]:
    auth_redirect = _login_required_redirect(request)
    if auth_redirect:
        raise HTTPException(status_code=401, detail="Login required")
    user_key = _user_key(request)
    counts = db.takeout_subscription_counts(user_key)
    pending = db.pending_takeout_subscriptions(user_key, limit)
    return {"ok": True, "counts": counts, "pending": _json_ready(pending)}


@app.post("/api/takeout/subscriptions/apply", tags=["recommendations"], summary="Subscribe to imported channels")
def api_apply_takeout_subscriptions(request: Request, limit: int = 50) -> dict[str, Any]:
    """Subscribe locally to channels the archive says you follow.

    Deliberately batched and deliberately not automatic on upload. This writes real
    subscription rows, and an archive can name several hundred channels — creating all
    of them because somebody uploaded a file is not a decision to make on their behalf.
    """
    auth_redirect = _login_required_redirect(request)
    if auth_redirect:
        raise HTTPException(status_code=401, detail="Login required")

    user_key = _user_key(request)
    batch = db.pending_takeout_subscriptions(user_key, max(1, min(limit, 200)))
    if not batch:
        return {"ok": True, "subscribed": 0, "failed": 0, "counts": db.takeout_subscription_counts(user_key), "done": True}

    done: list[str] = []
    failed: list[dict[str, str]] = []
    for item in batch:
        channel_id = str(item.get("youtube_channel_id") or "")
        title = str(item.get("title") or channel_id)
        feed_url = _youtube_feed_url(channel_id)
        try:
            stored = _subscribe_channel_local(channel_id, title, feed_url)
        except Exception as exc:  # noqa: BLE001 - one bad channel must not stop the batch
            message = str(exc)
            logger.info("takeout subscribe failed for %s: %s", channel_id, message)
            # A channel deleted on YouTube has no feed to fetch, so it will fail
            # forever. Mark it rather than retrying it on every pass, which is
            # what made the button sit there offering the same handful of channels.
            permanent = "400" in message or "404" in message or isinstance(exc, ValueError)
            if permanent:
                db.mark_takeout_subscription_failed(channel_id, message, user_key)
            failed.append({
                "youtube_channel_id": channel_id, "title": title,
                "error": message[:160], "permanent": permanent,
            })
            continue
        # Subscribed, but a gone feed (deleted channel) would otherwise sit in the
        # pending list forever: the immediate fetch already proved it.
        feed_status = stored.get("feed_status")
        if feed_status in (400, 404, 410):
            reason = f"feed returned HTTP {feed_status}"
            db.mark_takeout_subscription_failed(channel_id, reason, user_key)
            failed.append({
                "youtube_channel_id": channel_id, "title": title,
                "error": reason, "permanent": True,
            })
            continue
        done.append(channel_id)

    db.mark_takeout_subscribed(done, user_key)
    # Newly subscribed channels have no avatar yet, which is most of why a freshly
    # imported list looks empty. Paced, on a thread, so the response is not held open.
    if done:
        threading.Thread(target=lambda: enrich_channels_paced(len(done)), name="channel-art", daemon=True).start()
    counts = db.takeout_subscription_counts(user_key)
    return {"ok": True, "subscribed": len(done), "failed": len(failed), "errors": failed[:10],
            "counts": counts, "done": counts["pending"] == 0}


@app.post("/api/feed/refresh", tags=["recommendations"], summary="Rebuild the ranked feed")
def api_refresh_feed(request: Request, vectors: bool = True, grow: bool = True) -> dict[str, Any]:
    """Recompute everything the feed is ranked from, now.

    The worker does this on its own schedule, which is fine day to day and useless right
    after an import: thousands of new watch events land and the feed keeps serving a
    profile built before any of them existed.
    """
    user_key = _user_key(request)
    result: dict[str, Any] = {"ok": True}
    if grow:
        # Ranking cannot surface a subject the library has no videos about, so a refresh
        # fetches before it re-ranks.
        result["growth"] = discovery.grow_library(user_key)
        # Growing the library adds videos dated from relative text ("2 days ago"), which
        # is wrong by up to a year and which the recency boost then rewards. Re-reading a
        # batch here means the dates converge as the feed is used rather than needing a
        # manual repair; the worker runs the same pass on its own schedule.
        from . import enrichment as _enrichment

        try:
            result["publish_dates"] = _enrichment.verify_publish_dates()
        except Exception as exc:  # noqa: BLE001
            result["publish_dates"] = {"ok": False, "message": str(exc)[:200]}
    if vectors:
        from . import topics

        topics.clear_cache()
        result["vectors"] = topics.rebuild_vectors(1200)
    # Both profiles are cached; without clearing them the rebuild above changes nothing
    # that the next page render can see.
    db.clear_watch_time_cache(user_key)
    db.clear_takeout_cache(user_key)
    from . import topics as _topics

    result["profile_terms"] = len(_topics.profile_vector(user_key))
    result["takeout_channels"] = len((db.takeout_profile(user_key).get("channels") or {}))
    return result


@app.post("/api/takeout/subscriptions/retry", tags=["recommendations"], summary="Retry failed subscriptions")
def api_retry_takeout_subscriptions(request: Request) -> dict[str, Any]:
    """Clear permanent-failure marks so those channels are offered again."""
    auth_redirect = _login_required_redirect(request)
    if auth_redirect:
        raise HTTPException(status_code=401, detail="Login required")
    user_key = _user_key(request)
    return {"ok": True, "cleared": db.retry_failed_takeout_subscriptions(user_key),
            "counts": db.takeout_subscription_counts(user_key)}


@app.get("/api/sponsorblock/{youtube_video_id}", tags=["playback"], summary="SponsorBlock segments")
def api_sponsorblock_segments(request: Request, youtube_video_id: str) -> dict[str, Any]:
    """Segments for one video, looked up server-side by hash prefix.

    The browser never talks to SponsorBlock: it asks this server, and this server asks
    about 65,536 videos at once. That is what keeps the lookup from identifying either the
    viewer or the video.
    """
    auth_redirect = _login_required_redirect(request)
    if auth_redirect:
        raise HTTPException(status_code=401, detail="Login required")
    from . import sponsorblock

    return sponsorblock.segments_for(youtube_video_id, _user_key(request))


@app.get("/api/status/panel", response_class=HTMLResponse, tags=["status"], summary="Status pane markup")
def api_status_panel(request: Request):
    """The status body, fetched when the pane is opened.

    Rendering it inline meant running live health checks against Postgres, Redis,
    the RSS poller and Invidious on *every* page load, because the settings dialog is part of
    the app shell. That alone took home renders from ~0.35s to ~1.4s.
    """
    auth_redirect = _login_required_redirect(request)
    if auth_redirect:
        return auth_redirect
    return HTMLResponse(_status_page_html(_user_key(request)))


@app.get("/api/feed/composition", tags=["recommendations"], summary="What the feed is actually made of")
def api_feed_composition(request: Request, depth: int = 60) -> dict[str, Any]:
    """Measured composition, signal coverage and the creators filling the feed.

    Served on demand rather than rendered into every page: producing it means running a
    full ranking pass, which is far too expensive to spend on a dialog most page loads
    never open.
    """
    auth_redirect = _login_required_redirect(request)
    if auth_redirect:
        raise HTTPException(status_code=401, detail="Login required")
    from . import evaluation

    user_key = _user_key(request)
    depth = max(20, min(depth, 120))
    coverage = evaluation.coverage_report(user_key, depth)
    muted = {str(c).lower() for c in (db.get_feed_preferences(user_key).get("muted_channels") or [])}
    return {
        "ok": True,
        "coverage": coverage,
        "channels": [
            {**row, "muted": str(row["key"]).lower() in muted}
            for row in db.top_feed_channels(user_key, 14)
        ],
    }


@app.get("/api/feed/preferences")
def api_feed_preferences(request: Request) -> dict[str, Any]:
    return {"ok": True, "preferences": _json_ready_value(db.get_feed_preferences(_user_key(request)))}


@app.post("/api/feed/preferences")
def api_update_feed_preferences(request: Request, payload: FeedPreferencesRequest) -> dict[str, Any]:
    """Apply a partial update, filling anything omitted from what is already stored."""
    user_key = _user_key(request)
    current = db.get_feed_preferences(user_key)

    def keep(value: Any, key: str) -> Any:
        return current.get(key) if value is None else value

    preferences = db.set_feed_preferences(
        preferred_topics=keep(payload.preferred_topics, "preferred_topics"),
        muted_topics=keep(payload.muted_topics, "muted_topics"),
        prioritize_subscriptions=keep(payload.prioritize_subscriptions, "prioritize_subscriptions"),
        show_comments=keep(payload.show_comments, "show_comments"),
        ryd_enabled=keep(payload.ryd_enabled, "ryd_enabled"),
        sponsorblock_enabled=keep(payload.sponsorblock_enabled, "sponsorblock_enabled"),
        sponsorblock_actions=keep(payload.sponsorblock_actions, "sponsorblock_actions"),
        show_posts_in_feeds=keep(payload.show_posts_in_feeds, "show_posts_in_feeds"),
        muted_channels=keep(payload.muted_channels, "muted_channels"),
        exclude_shorts=keep(payload.exclude_shorts, "exclude_shorts"),
        discovery_level=keep(payload.discovery_level, "discovery_level"),
        user_key=user_key,
    )
    return {"ok": True, "preferences": _json_ready_value(preferences)}


@app.get("/watch/{youtube_video_id}/local", response_class=HTMLResponse)
def watch_local_player(request: Request, youtube_video_id: str, autoplay: str = "1", t: int = 0):
    """Plays the downloaded file. No network involved beyond this server."""
    auth_redirect = _login_required_redirect(request)
    if auth_redirect:
        return auth_redirect
    record = db.get_download(youtube_video_id, _user_key(request))
    if not record:
        raise HTTPException(status_code=404, detail="Not downloaded")
    video_id = escape(youtube_video_id, quote=True)
    play = " autoplay" if autoplay == "1" else ""
    start = max(0, min(int(t or 0), 86400))
    quality = escape(str(record.get("quality") or ""))
    note = f"Downloaded &middot; {quality}" if quality else "Downloaded"
    return HTMLResponse(f'''<!doctype html><html lang="en"><head><meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1"><title>OpenFeed player</title>
      <style>
        html, body {{ margin: 0; width: 100%; height: 100%; background: #000; }}
        video {{ width: 100%; height: 100%; display: block; background: #000; }}
        .note {{ position: absolute; left: 10px; bottom: 10px; padding: 4px 9px; border-radius: 999px;
          background: rgba(10,10,11,.72); color: #ededef; font: 11px system-ui, sans-serif; pointer-events: none; }}
      </style></head><body>
      <video controls playsinline preload="metadata"{play} src="/api/downloads/{video_id}/file#t={start}"></video>
      <span class="note">{note}</span>
    </body></html>''')


@app.get("/watch/{youtube_video_id}/ytdlp", response_class=HTMLResponse)
def watch_ytdlp_player(request: Request, youtube_video_id: str, autoplay: str = "1", t: int = 0):
    """The yt-dlp fallback player: a plain <video> over our own stream proxy.

    This is the last rung before handing the viewer to YouTube's own embed. It plays the
    muxed rendition, which is whatever YouTube still publishes as a single file — usually
    360p, sometimes 720p. That is a real quality ceiling, and the page says so rather
    than letting it look like a bug.
    """
    auth_redirect = _login_required_redirect(request)
    if auth_redirect:
        return auth_redirect
    video_id = escape(youtube_video_id, quote=True)
    play = " autoplay" if autoplay == "1" else ""
    quality = ""
    try:
        quality = escape(str(ytdlp.playback_stream(youtube_video_id).get("quality") or ""))
    except Exception as exc:
        logger.info("yt-dlp could not resolve a stream for %s: %s", youtube_video_id, exc)
        return HTMLResponse(
            f'<!doctype html><meta charset="utf-8"><style>html,body{{margin:0;height:100%;background:#000;color:#a8a8a8;'
            f'display:grid;place-items:center;font:13px/1.5 system-ui,sans-serif;text-align:center;padding:16px}}</style>'
            f'<p>yt-dlp could not extract a playable stream for this video.</p>',
            status_code=200,
        )
    note = f"Direct stream &middot; {quality}" if quality else "Direct stream"
    # A media-fragment offset is the one way to start a plain <video> partway in
    # without waiting for metadata and seeking afterwards.
    start = max(0, min(int(t or 0), 86400))
    return HTMLResponse(f'''<!doctype html><html lang="en"><head><meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1"><title>OpenFeed player</title>
      <style>
        html, body {{ margin: 0; width: 100%; height: 100%; background: #000; }}
        video {{ width: 100%; height: 100%; display: block; background: #000; }}
        .note {{ position: absolute; left: 10px; bottom: 10px; padding: 4px 9px; border-radius: 999px;
          background: rgba(10,10,11,.72); color: #ededef; font: 11px system-ui, sans-serif; pointer-events: none; }}
      </style></head><body>
      <video controls playsinline preload="metadata"{play} src="/api/ytdlp/stream/{video_id}#t={start}"></video>
      <span class="note">{note}</span>
    </body></html>''')


@app.get("/api/ytdlp/stream/{youtube_video_id}")
def api_ytdlp_stream(request: Request, youtube_video_id: str):
    """Proxy the yt-dlp-extracted stream, preserving range requests.

    Proxying rather than redirecting keeps the viewer's IP away from Google, the same way
    thumbnails and avatars are proxied — and it is also the only thing that works, since
    the signed URLs are bound to the address that requested them.
    """
    auth_redirect = _login_required_redirect(request)
    if auth_redirect:
        return auth_redirect
    try:
        stream = ytdlp.playback_stream(youtube_video_id)
    except ytdlp.YtdlpError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    upstream_headers = {"user-agent": "Mozilla/5.0"}
    # Seeking is the whole reason to forward this: without it the browser has to fetch
    # the file from the start to reach any point in the timeline.
    if request.headers.get("range"):
        upstream_headers["range"] = request.headers["range"]

    client = _invidious_client()
    # The pooled client does not follow redirects, which is right for Invidious assets but
    # wrong here: googlevideo answers with a 302 to a different host, and handing that back
    # would send the browser straight to Google — leaking the address this proxy exists to
    # keep private, while looking like it worked.
    upstream = client.send(
        client.build_request("GET", str(stream["stream_url"]), headers=upstream_headers),
        stream=True,
        follow_redirects=True,
    )
    if upstream.status_code >= 400:
        upstream.close()
        mark_source_result("ytdlp", False)
        raise HTTPException(status_code=502, detail=f"The stream host returned HTTP {upstream.status_code}")
    mark_source_result("ytdlp", True)

    headers = {"accept-ranges": "bytes", "cache-control": "no-store"}
    for name in ("content-length", "content-range", "content-type"):
        if upstream.headers.get(name):
            headers[name] = upstream.headers[name]
    headers.setdefault("content-type", str(stream.get("stream_type") or "video/mp4"))
    return StreamingResponse(
        upstream.iter_bytes(),
        status_code=upstream.status_code,
        headers=headers,
        background=BackgroundTask(upstream.close),
    )


PLAYBACK_PROBE_VIDEO = "dQw4w9WgXcQ"


def _playback_probe_video() -> str:
    """Probe with something from the library, not a video Invidious already cached.

    A cached video answers from Invidious's own database and would report healthy even
    while every live fetch is failing — which is exactly the state to catch.
    """
    try:
        for row in db.list_recent_videos(8):
            video_id = str(row.get("youtube_video_id") or "")
            if video_id:
                return video_id
    except Exception:
        pass
    return PLAYBACK_PROBE_VIDEO


def _probe_playback_source(source: dict[str, str], video_id: str) -> dict[str, Any]:
    """Ask one source whether it can actually serve a player, and record the result."""
    started = time.time()
    if source["kind"] == "invidious-local":
        ok, detail = _embed_playable(video_id, use_cache=False)
    else:
        url = _source_url(source, urllib.parse.quote(video_id, safe=""), "0")
        if source["kind"] == "youtube":
            # The no-cookie embed is a static Google endpoint; reachability is the check.
            url = f"https://www.youtube-nocookie.com/embed/{urllib.parse.quote(video_id, safe='')}"
        try:
            response = httpx.get(url, timeout=httpx.Timeout(8.0, connect=4.0), follow_redirects=True, headers={"user-agent": "Mozilla/5.0"})
            ok = response.status_code == 200
            detail = "" if ok else f"HTTP {response.status_code}"
        except Exception as exc:
            ok, detail = False, type(exc).__name__
    mark_source_result(source["id"], ok)
    return {
        "id": source["id"],
        "label": source["label"],
        "kind": source["kind"],
        "ok": ok,
        "detail": detail,
        "latency_ms": int((time.time() - started) * 1000),
        "health": source_health_snapshot(source["id"]),
    }


def _diagnose_playback(video_id: str) -> dict[str, Any]:
    """Walk the playback chain and report the first hop that breaks.

    Every failure mode below produced a real outage on this deployment, so the messages
    name the actual cause instead of the generic "something went wrong" the player shows.
    """
    settings = get_settings()
    steps: list[dict[str, Any]] = []

    def step(name: str, ok: bool, detail: str = "") -> None:
        steps.append({"name": name, "ok": ok, "detail": detail})

    if not settings.invidious_base_url:
        return {"ok": False, "code": "not-configured", "steps": steps,
                "headline": "No Invidious backend is configured.",
                "explanation": "OpenFeed has no INVIDIOUS_BASE_URL, so the private playback path does not exist.",
                "remedies": ["Set INVIDIOUS_BASE_URL and restart OpenFeed.", "Until then, enable the YouTube fallback in Settings."]}

    client = _invidious_client()
    timeout = httpx.Timeout(10.0, connect=4.0)

    try:
        client.get(f"{settings.invidious_base_url}/api/v1/trending", timeout=timeout)
        step("Invidious reachable", True)
    except Exception as exc:
        step("Invidious reachable", False, type(exc).__name__)
        return {"ok": False, "code": "backend-unreachable", "steps": steps,
                "headline": "OpenFeed cannot reach your Invidious instance.",
                "explanation": "The container is down, restarting, or not on the same Docker network.",
                "remedies": ["Check that openfeed-invidious is running.", "Look at its logs for a crash on startup."]}

    try:
        embed = client.get(f"{settings.invidious_base_url}/embed/{video_id}", timeout=timeout, follow_redirects=True)
    except Exception as exc:
        step("Player page", False, type(exc).__name__)
        return {"ok": False, "code": "embed-error", "steps": steps,
                "headline": "The playback backend did not answer.",
                "explanation": str(exc), "remedies": ["Try again in a moment."]}

    if embed.status_code == 503 or "valid potoken" in embed.text.lower():
        step("Player page", False, "companion has no token yet")
        return {"ok": False, "code": "token-warming", "steps": steps,
                "headline": "The playback backend is still starting up.",
                "explanation": "invidious-companion is negotiating a session token with YouTube. This normally takes under a minute after a restart.",
                "remedies": ["Wait about a minute and try again.", "OpenFeed will use your fallback source in the meantime."]}

    if embed.status_code != 200 or "<video" not in embed.text:
        step("Player page", False, f"HTTP {embed.status_code}")
        return {"ok": False, "code": "no-formats", "steps": steps,
                "headline": "YouTube refused to hand over this video's streams.",
                "explanation": "Invidious asked YouTube for playable formats and got none. YouTube rejects these requests when the attestation token cannot be validated — most often because the traffic leaves through a VPN, a datacenter, or a shared/flagged IP address.",
                "remedies": [
                    "If you are on a VPN, turn it off for this machine (or split-tunnel Docker) and try again.",
                    "Set COMPANION_PROXY to an egress YouTube trusts.",
                    "Set COMPANION_YOUTUBE_COOKIES to a signed-in YouTube session.",
                    "Leave the YouTube fallback enabled so playback keeps working meanwhile.",
                ]}
    step("Player page", True)

    manifest_match = re.search(r'<source src="([^"]*manifest/dash[^"]*)"', embed.text)
    if not manifest_match:
        step("Stream manifest", False, "no DASH manifest in the player page")
        return {"ok": False, "code": "no-manifest", "steps": steps,
                "headline": "The player page came back without a stream manifest.",
                "explanation": "Invidious rendered a player but no media source, which usually means the companion returned an empty player response.",
                "remedies": ["Check the invidious-companion logs.", "Restart openfeed-invidious-companion."]}

    manifest_path = unescape(manifest_match.group(1))
    try:
        manifest = client.get(f"{settings.invidious_base_url}{manifest_path}", timeout=timeout, follow_redirects=True)
        manifest.raise_for_status()
        step("Stream manifest", True)
    except Exception as exc:
        step("Stream manifest", False, type(exc).__name__)
        return {"ok": False, "code": "manifest-error", "steps": steps,
                "headline": "The stream manifest could not be loaded.",
                "explanation": str(exc), "remedies": ["Restart openfeed-invidious-companion."]}

    segment_match = re.search(r"<BaseURL>([^<]+)</BaseURL>", manifest.text)
    if not segment_match:
        step("Media segment", False, "manifest has no media URLs")
        return {"ok": False, "code": "empty-manifest", "steps": steps,
                "headline": "The stream manifest is empty.",
                "explanation": "YouTube returned a manifest with no media in it.",
                "remedies": ["Try another video — this one may be private, age-restricted, or region-locked."]}

    segment_url = f"{settings.invidious_base_url}{unescape(segment_match.group(1))}"
    try:
        segment = client.get(segment_url, timeout=timeout, follow_redirects=False, headers={"range": "bytes=0-4095"})
    except Exception as exc:
        step("Media segment", False, type(exc).__name__)
        return {"ok": False, "code": "segment-error", "steps": steps,
                "headline": "The video data could not be fetched.",
                "explanation": str(exc), "remedies": ["Try again in a moment."]}

    content_type = segment.headers.get("content-type", "")
    if "ump" in content_type or not segment.content:
        step("Media segment", False, f"{segment.status_code} {content_type or 'empty body'}")
        return {"ok": False, "code": "ump-mismatch", "steps": steps,
                "headline": "The backend is returning a format the player cannot read.",
                "explanation": "Media came back as UMP (or empty) instead of MP4. The browser player reports this as “media could not be loaded … format is not supported”.",
                "remedies": ["Set COMPANION_UMP=false and restart openfeed-invidious-companion."]}

    if segment.status_code in {401, 403}:
        step("Media segment", False, f"HTTP {segment.status_code}")
        return {"ok": False, "code": "stream-forbidden", "steps": steps,
                "headline": "YouTube rejected the stream URL.",
                "explanation": "Stream URLs are signed for one IP address. A 403 usually means your connection changed between the URL being issued and the video being played — toggling a VPN mid-session does exactly this.",
                "remedies": ["Reload the page to get freshly signed URLs.", "Keep the VPN in one state while watching."]}

    if segment.status_code >= 400:
        step("Media segment", False, f"HTTP {segment.status_code}")
        return {"ok": False, "code": "stream-error", "steps": steps,
                "headline": "YouTube returned an error for the video data.",
                "explanation": f"The media request came back HTTP {segment.status_code}.",
                "remedies": ["Try again, or try a different video."]}

    step("Media segment", True, content_type)
    return {"ok": True, "code": "healthy", "steps": steps,
            "headline": "Local playback is working.",
            "explanation": "Invidious served a player, a manifest, and real media data.",
            "remedies": []}


@app.get("/api/playback/diagnose")
def api_playback_diagnose(video_id: str = "") -> dict[str, Any]:
    return {"ok": True, "diagnosis": _diagnose_playback(video_id.strip() or _playback_probe_video())}


@app.get("/api/playback/sources")
def api_playback_sources(request: Request) -> dict[str, Any]:
    user_key = _user_key(request)
    settings = get_settings()
    sources = [
        {**source, "health": source_health_snapshot(source["id"]), "in_cooldown": source_in_cooldown(source["id"])}
        for source in _all_playback_sources(user_key)
    ]
    return {
        "ok": True,
        "preferences": _json_ready_value(db.get_playback_preferences(user_key)),
        "sources": sources,
        "deployment_fallbacks": settings.invidious_fallback_list,
        "youtube_fallback_allowed": settings.youtube_fallback_enabled,
        "local_backend_degraded": playback_backend_degraded(),
    }


@app.post("/api/playback/sources/test")
def api_playback_test(request: Request) -> dict[str, Any]:
    user_key = _user_key(request)
    probe_video = _playback_probe_video()
    results = [_probe_playback_source(source, probe_video) for source in _configured_playback_sources(user_key)]
    return {"ok": True, "video_id": probe_video, "results": results}


@app.post("/api/playback/report")
def api_playback_report(payload: PlaybackReportRequest) -> dict[str, Any]:
    """The player tells us what really happened, which beats any probe."""
    mark_source_result(payload.source_id, payload.ok)
    return {"ok": True, "health": source_health_snapshot(payload.source_id)}


@app.get("/api/playback/preferences")
def api_playback_preferences(request: Request) -> dict[str, Any]:
    return {"ok": True, "preferences": _json_ready_value(db.get_playback_preferences(_user_key(request)))}


@app.post("/api/playback/preferences")
def api_update_playback_preferences(request: Request, payload: PlaybackPreferencesRequest) -> dict[str, Any]:
    preferences = db.set_playback_preferences(
        prefer_local=payload.prefer_local,
        public_invidious_enabled=payload.public_invidious_enabled,
        public_invidious_urls=payload.public_invidious_urls,
        youtube_fallback=payload.youtube_fallback,
        source_order=payload.source_order,
        disabled_sources=payload.disabled_sources,
        user_key=_user_key(request),
    )
    return {"ok": True, "preferences": _json_ready_value(preferences)}


@app.get("/api/recommendations/library")
def api_library_recommendations(request: Request, limit: int = 50) -> dict[str, Any]:
    return {"items": _json_ready(db.list_subscription_recommendations(max(1, min(limit, 200)), _user_key(request)))}


@app.get("/api/library")
def api_library(request: Request, limit: int = 80) -> dict[str, Any]:
    return {"items": _json_ready(db.list_library_activity(max(1, min(limit, 200)), _user_key(request)))}


@app.get("/api/channels")
def api_channels(limit: int = 200) -> dict[str, Any]:
    return {"items": _json_ready(db.list_channels(max(1, min(limit, 500))))}


@app.get("/api/channels/{youtube_channel_id}")
def api_channel(youtube_channel_id: str) -> dict[str, Any]:
    channel = _json_ready_one(db.get_channel_by_youtube_id(youtube_channel_id))
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    return {"item": channel}


@app.get("/api/channels/{youtube_channel_id}/videos")
def api_channel_videos(youtube_channel_id: str, limit: int = 80, offset: int = 0, sort: str = "latest") -> dict[str, Any]:
    sort_key = sort if sort in {"latest", "popular", "oldest"} else "latest"
    return {"items": _json_ready(db.list_channel_videos(youtube_channel_id, max(1, min(limit, 200)), sort_key, max(0, offset)))}


def _fetch_single_feed_best_effort(feed_url: str, youtube_channel_id: str | None = None) -> int | None:
    """Pull one channel feed right now; never raises, returns the HTTP status.

    A new subscription would otherwise sit empty until the worker's next pass.
    Every failure mode — network, malformed feed, database — is swallowed and
    logged: the subscription row already exists, so the next sync recovers.
    The status lets callers (Takeout) tell a dead feed from a slow network.
    """
    try:
        settings = get_settings()
        with httpx.Client(timeout=20.0, follow_redirects=True, headers={"User-Agent": "OpenFeed RSS poller"}) as client:
            result = fetch_channel_feed(client, feed_url)
        if result.status != 200 or not result.text:
            return result.status
        for entry in parse_channel_feed(result.text, feed_url):
            channel_id = db.upsert_channel(
                entry["channel_title"], entry.get("youtube_channel_id"), entry.get("feed_url")
            )
            video_id = db.upsert_video(entry, channel_id, entry["youtube_video_id"])
            entry_channel = entry.get("youtube_channel_id")
            subscription_id = db.subscription_id_for_channel(entry_channel) if entry_channel else None
            entry["subscription_id"] = subscription_id
            db.upsert_entry(entry, video_id, subscription_id)
        subscription_id = db.subscription_id_for_channel(youtube_channel_id) if youtube_channel_id else None
        if subscription_id:
            db.record_rss_fetch(
                subscription_id,
                etag=result.etag,
                last_modified=result.last_modified,
                ok=True,
                ttl_seconds=settings.rss_poll_ttl_seconds,
            )
        return result.status
    except Exception as exc:  # noqa: BLE001 - the subscribe already succeeded
        logger.info("single-feed fetch skipped for %s: %s", feed_url, exc)
        return None


def _subscribe_channel_local(
    youtube_channel_id: str = "", title: str = "", feed_url: str = ""
) -> dict[str, Any]:
    """Create the local subscription row for a channel. No remote service involved.

    The channel id is resolved from any of the inputs (a bare channel id, a feed
    URL carrying ``?channel_id=``, or a title mentioning one) and the channel
    RSS feed URL is derived from it. A bare id that is not a well-formed channel
    id is rejected before it can mint a junk row; a bare feed URL (OPML, Takeout,
    non-YouTube feeds) is always accepted and fetched best-effort. The
    freshrss_subscriptions row is upserted under a ``feed/<url>`` id, which the
    dedupe pass collapses if the poller later meets the same feed under another
    id. Then that one feed is pulled immediately on a best-effort basis; a fetch
    failure never fails the subscribe. Returns the stored feed_url,
    youtube_channel_id, title and the immediate fetch's HTTP status (None when
    the fetch was skipped or never answered).
    """
    given_id = (youtube_channel_id or "").strip()
    given_url = (feed_url or "").strip()
    if given_id and not given_url and not CHANNEL_ID_STRICT_RE.match(given_id):
        raise ValueError("That does not look like a YouTube channel ID")
    resolved_id = (
        given_id
        or youtube_channel_id_from_text(feed_url)
        or youtube_channel_id_from_text(title)
        or ""
    )
    if resolved_id and CHANNEL_ID_STRICT_RE.match(resolved_id):
        # Canonical form: drops http/trailing-slash variants of the same feed so
        # re-subscribing cannot mint a second row for the same channel.
        url = _youtube_feed_url(resolved_id)
    else:
        url = given_url or (_youtube_feed_url(resolved_id) if resolved_id else "")
    if not url:
        raise ValueError("A feed URL or YouTube channel ID is required")
    name = (title or "").strip() or resolved_id or url
    db.upsert_subscription(
        {
            "id": f"feed/{url}",
            "title": name,
            "url": url,
            "htmlUrl": f"https://www.youtube.com/channel/{resolved_id}" if resolved_id else None,
            "categories": [],
        },
        resolved_id or None,
    )
    feed_status: int | None = None
    if resolved_id:
        db.upsert_channel(name, resolved_id, url)
        feed_status = _fetch_single_feed_best_effort(url, resolved_id)
        # One channel, off-thread: the subscribe response should not wait on artwork.
        threading.Thread(target=lambda: enrich_channel(resolved_id), name="channel-art", daemon=True).start()
    return {"youtube_channel_id": resolved_id or None, "feed_url": url, "title": name, "feed_status": feed_status}


@app.post("/api/subscriptions/freshrss")
def api_subscribe_freshrss(payload: SubscribeRequest) -> dict[str, Any]:
    youtube_channel_id = payload.youtube_channel_id.strip()
    feed_url = payload.feed_url.strip() or (_youtube_feed_url(youtube_channel_id) if youtube_channel_id else "")
    if not feed_url:
        raise HTTPException(status_code=400, detail="A feed URL or YouTube channel ID is required")
    # A bare id mints the feed URL by itself, so a malformed one must fail here
    # instead of creating a junk row. (A bare feed URL still goes through and
    # fails best-effort: OPML/Takeout/non-YouTube feeds.)
    if youtube_channel_id and not payload.feed_url.strip() and not CHANNEL_ID_STRICT_RE.match(youtube_channel_id):
        raise HTTPException(status_code=400, detail="That does not look like a YouTube channel ID")

    title = payload.title.strip() or youtube_channel_id or feed_url
    try:
        stored = _subscribe_channel_local(youtube_channel_id, title, feed_url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "feed_url": stored["feed_url"], "youtube_channel_id": stored["youtube_channel_id"], "title": stored["title"]}


@app.delete("/api/subscriptions/freshrss")
def api_unsubscribe_freshrss(payload: SubscribeRequest) -> dict[str, Any]:
    youtube_channel_id = payload.youtube_channel_id.strip()
    feed_url = payload.feed_url.strip() or (_youtube_feed_url(youtube_channel_id) if youtube_channel_id else "")
    if not feed_url:
        raise HTTPException(status_code=400, detail="A feed URL or YouTube channel ID is required")

    # Local row only: the channel's videos and watch history stay, so
    # unsubscribing never deletes anything the user has already seen.
    removed = db.remove_subscription(feed_url, youtube_channel_id or None)
    if not removed:
        raise HTTPException(status_code=404, detail="No such subscription")
    return {"ok": True, "feed_url": feed_url, "youtube_channel_id": youtube_channel_id or None, "removed_local": removed}


@app.get("/api/subscriptions")
def api_subscriptions(limit: int = 500) -> dict[str, Any]:
    return {"items": _json_ready(db.list_subscriptions(max(1, min(limit, 1000))))}


@app.get("/api/subscription-collections")
def api_subscription_collections(request: Request) -> dict[str, Any]:
    return {"items": _json_ready_value(db.list_subscription_collections(_user_key(request)))}


@app.get("/api/subscription-collections/memberships")
def api_collection_memberships(request: Request) -> dict[str, Any]:
    """subscription id -> collection ids, so the UI can show membership without N requests."""
    return {"items": db.list_collection_ids_for_subscriptions(_user_key(request))}


@app.post("/api/subscription-collections")
def api_create_subscription_collection(request: Request, payload: SubscriptionCollectionRequest) -> dict[str, Any]:
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Collection name is required")
    collection = db.create_subscription_collection(name, payload.description, _user_key(request))
    return {"ok": True, "collection": _json_ready_value(collection)}


@app.patch("/api/subscription-collections/{collection_id}")
def api_update_subscription_collection(request: Request, collection_id: str, payload: SubscriptionCollectionRequest) -> dict[str, Any]:
    if not _valid_uuid(collection_id):
        raise HTTPException(status_code=404, detail="Collection not found")
    collection = db.update_subscription_collection(collection_id, payload.name.strip(), payload.description, _user_key(request))
    if not collection:
        raise HTTPException(status_code=404, detail="Collection not found")
    return {"ok": True, "collection": _json_ready_value(collection)}


@app.delete("/api/subscription-collections/{collection_id}")
def api_delete_subscription_collection(request: Request, collection_id: str) -> dict[str, Any]:
    if not _valid_uuid(collection_id):
        raise HTTPException(status_code=404, detail="Collection not found")
    deleted = db.delete_subscription_collection(collection_id, _user_key(request))
    if not deleted:
        raise HTTPException(status_code=404, detail="Collection not found")
    return {"ok": True}


@app.get("/api/subscription-collections/{collection_id}/subscriptions")
def api_collection_subscriptions(request: Request, collection_id: str) -> dict[str, Any]:
    return {"items": _json_ready_value(db.list_collection_subscriptions(collection_id, _user_key(request)))}


@app.get("/api/subscription-collections/{collection_id}/videos")
def api_collection_videos(request: Request, collection_id: str, limit: int = 80) -> dict[str, Any]:
    return {"items": _json_ready_value(db.list_collection_videos(collection_id, max(1, min(limit, 200)), _user_key(request)))}


@app.post("/api/subscription-collections/{collection_id}/members")
def api_add_subscription_collection_member(request: Request, collection_id: str, payload: SubscriptionCollectionMemberRequest) -> dict[str, Any]:
    item = db.add_subscription_to_collection(collection_id, payload.subscription_id.strip(), _user_key(request))
    if not item:
        raise HTTPException(status_code=404, detail="Collection or subscription not found")
    return {"ok": True, "item": _json_ready_value(item)}


@app.delete("/api/subscription-collections/{collection_id}/members", tags=["subscriptions"], summary="Remove a collection member")
def api_remove_subscription_collection_member_body(
    request: Request, collection_id: str, payload: SubscriptionCollectionMemberRequest,
) -> dict[str, Any]:
    """Remove a member of the caller's collection using its exact FreshRSS identifier.

    Send JSON `{"subscription_id": "feed/4"}`; no URL encoding is needed. Returns
    `{"ok": true}`, or 404 when the member is absent or the collection belongs to
    another user. The path-based endpoint remains available as a compatibility alias.
    """
    return api_remove_subscription_collection_member(request, collection_id, payload.subscription_id)


@app.delete("/api/subscription-collections/{collection_id}/members/{subscription_id:path}")
def api_remove_subscription_collection_member(request: Request, collection_id: str, subscription_id: str) -> dict[str, Any]:
    # Local subscription ids contain a slash ("feed/4"), so this segment must be a path param.
    deleted = db.remove_subscription_from_collection(collection_id, subscription_id, _user_key(request))
    if not deleted:
        raise HTTPException(status_code=404, detail="Collection member not found")
    return {"ok": True}


@app.get("/api/sync/runs")
def api_sync_runs(limit: int = 20) -> dict[str, Any]:
    return {"items": _json_ready(db.list_sync_runs(max(1, min(limit, 100))))}


@app.post("/api/discover/related")
def api_discover_related(seed_limit: int = 3, per_seed: int = 6) -> dict[str, Any]:
    return discover_related_videos(max(1, min(seed_limit, 10)), max(1, min(per_seed, 12)))


@app.get("/api/discovery")
def api_discovery(request: Request, limit: int = 50) -> dict[str, Any]:
    return {"items": _json_ready(db.list_discovery_videos(max(1, min(limit, 200)), _user_key(request)))}


@app.get("/api/enrich/status")
def api_enrich_status() -> dict[str, Any]:
    return db.enrichment_status()


@app.post("/api/enrich/channels")
def api_enrich_channels(limit: int = 10, refresh: bool = False) -> dict[str, Any]:
    return enrich_channels(max(1, min(limit, 100)), refresh)


@app.post("/api/enrich/subscription-art", tags=["metadata"], summary="Fetch avatars for every subscribed channel")
def api_enrich_subscription_art(wait: bool = False) -> dict[str, Any]:
    """Force the sidebar's blank avatars to fill in now.

    Paced internally, so a 70-channel list takes a little under a minute. By default it
    runs on a thread and answers immediately with how many are still missing.
    """
    if wait:
        return backfill_subscription_art()
    missing = db.count_subscription_channels_missing_media()
    if missing:
        threading.Thread(target=backfill_subscription_art, name="subscription-art", daemon=True).start()
    return {"ok": True, "started": bool(missing), "missing": missing}


@app.post("/api/enrich/recent")
def api_enrich_recent(limit: int = 5) -> dict[str, Any]:
    return enrich_recent_videos(max(1, min(limit, 25)))


@app.post("/api/feedback")
def api_feedback(request: Request, feedback: FeedbackRequest) -> dict[str, Any]:
    event_type = feedback.event_type.strip()
    if event_type not in ALLOWED_FEEDBACK_EVENTS:
        raise HTTPException(status_code=400, detail="Unsupported feedback event type")
    youtube_video_id = feedback.youtube_video_id.strip()
    user_key = _user_key(request)
    event = db.record_video_event(youtube_video_id, event_type, feedback.payload, user_key)
    if not event:
        raise HTTPException(status_code=404, detail="Video not found")
    state = db.set_video_feedback(youtube_video_id, event_type, feedback.payload, user_key)
    if event_type == "watch_page_open":
        db.update_playback_progress(youtube_video_id, 0, None, "open", feedback.payload, user_key)
    if not state:
        state = db.get_video_feedback(youtube_video_id, user_key) or {}
    return {"ok": True, "event": _json_ready_value(event), "feedback": _json_ready_value(state)}


@app.post("/api/playback/progress")
def api_playback_progress(request: Request, progress: PlaybackProgressRequest) -> dict[str, Any]:
    user_key = _user_key(request)
    event_type = progress.event_type.strip() or "progress"
    youtube_video_id = progress.youtube_video_id.strip()
    state = db.update_playback_progress(
        youtube_video_id,
        progress.current_seconds,
        progress.duration_seconds,
        event_type,
        progress.payload,
        user_key,
        watch_delta_seconds=progress.watch_delta_seconds,
        resume_seconds=progress.resume_seconds,
    )
    # The ranking profile is aggregated and cached, so it has to be invalidated when new
    # watch time lands or the feed keeps ranking on a stale picture for two minutes.
    if progress.watch_delta_seconds:
        db.clear_watch_time_cache(user_key)
    if not state:
        raise HTTPException(status_code=404, detail="Video not found")
    return {"ok": True, "progress": _json_ready_value(state)}


@app.post("/api/playlists")
def api_create_playlist(request: Request, payload: PlaylistRequest) -> dict[str, Any]:
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Playlist name is required")
    return {"ok": True, "playlist": _json_ready_value(db.create_playlist(name, payload.description, _user_key(request)))}


@app.get("/api/playlists")
def api_playlists(request: Request) -> dict[str, Any]:
    return {"items": _json_ready_value(db.list_playlists(_user_key(request)))}


@app.patch("/api/playlists/{playlist_id}")
def api_update_playlist(request: Request, playlist_id: str, payload: PlaylistRequest) -> dict[str, Any]:
    if not _valid_uuid(playlist_id):
        raise HTTPException(status_code=404, detail="Playlist not found")
    playlist = db.update_playlist(playlist_id, payload.name.strip(), payload.description, _user_key(request))
    if not playlist:
        raise HTTPException(status_code=404, detail="Playlist not found")
    return {"ok": True, "playlist": _json_ready_value(playlist)}


@app.delete("/api/playlists/{playlist_id}")
def api_delete_playlist(request: Request, playlist_id: str) -> dict[str, Any]:
    if not _valid_uuid(playlist_id):
        raise HTTPException(status_code=404, detail="Playlist not found")
    deleted = db.delete_playlist(playlist_id, _user_key(request))
    if not deleted:
        raise HTTPException(status_code=404, detail="Playlist not found")
    return {"ok": True, "deleted": deleted}


@app.post("/api/playlists/{playlist_id}/items")
def api_add_playlist_item(request: Request, playlist_id: str, payload: PlaylistItemRequest) -> dict[str, Any]:
    if not _valid_uuid(playlist_id):
        raise HTTPException(status_code=404, detail="Playlist not found")
    item = db.add_video_to_playlist(playlist_id, payload.youtube_video_id.strip(), _user_key(request))
    if not item:
        raise HTTPException(status_code=404, detail="Playlist or video not found")
    return {"ok": True, "item": _json_ready_value(item)}


@app.get("/api/playlists/{playlist_id}/items")
def api_playlist_items(request: Request, playlist_id: str, limit: int = 200) -> dict[str, Any]:
    user_key = _user_key(request)
    if not _valid_uuid(playlist_id):
        raise HTTPException(status_code=404, detail="Playlist not found")
    playlist = db.get_playlist(playlist_id, user_key)
    if not playlist:
        raise HTTPException(status_code=404, detail="Playlist not found")
    videos = db.list_playlist_videos(playlist_id, max(1, min(limit, 500)), user_key)
    return {"playlist": _json_ready_one(playlist), "items": _json_ready(videos)}


@app.delete("/api/playlists/{playlist_id}/items/{youtube_video_id}")
def api_remove_playlist_item(request: Request, playlist_id: str, youtube_video_id: str) -> dict[str, Any]:
    if not _valid_uuid(playlist_id):
        raise HTTPException(status_code=404, detail="Playlist not found")
    removed = db.remove_video_from_playlist(playlist_id, youtube_video_id, _user_key(request))
    if not removed:
        raise HTTPException(status_code=404, detail="Playlist item not found")
    return {"ok": True, "removed": removed}


COMMENT_LIMIT = 40
COMMENT_REPLY_LIMIT = 60


def _proxied_avatar(url: Any) -> str:
    """A single avatar URL routed through our own proxy.

    creatorHeart gives one URL rather than the thumbnail list every other author field
    uses, so it needs its own path.
    """
    text = str(url or "")
    if not text:
        return ""
    # Hosts vary: yt3.ggpht.com/ytc/..., yt3.googleusercontent.com/..., and paths already
    # containing /ggpht/. Stripping the scheme and host covers all of them; matching on a
    # path marker alone missed yt3.ggpht.com and leaked the request to Google.
    for host in ("yt3.ggpht.com/", "yt4.ggpht.com/", "yt3.googleusercontent.com/", "googleusercontent.com/"):
        index = text.find(host)
        if index != -1:
            return "/ggpht/" + text[index + len(host):]
    index = text.find("/ggpht/")
    if index != -1:
        return "/ggpht/" + text[index + len("/ggpht/"):]
    return text


def _comment_avatar(thumbnails: Any) -> str:
    if not isinstance(thumbnails, list):
        return ""
    best = None
    for item in thumbnails:
        if not isinstance(item, dict) or not item.get("url"):
            continue
        width = int(item.get("width") or 0)
        if best is None or (width and width <= 76 and width > int(best.get("width") or 0)):
            best = item
    url = str((best or {}).get("url") or "")
    if not url:
        return ""
    for marker in ("/ggpht/", "googleusercontent.com/", "ggpht.com/"):
        index = url.find(marker)
        if index != -1:
            return "/ggpht/" + url[index + len(marker):]
    return ""


# Invidious's comments endpoint stopped returning authorThumbnails (empty for every
# comment), so avatars are resolved lazily against the channel endpoint, which still
# carries them. Hits live a day, misses ten minutes so a failed lookup does not pin
# initials on a commenter forever; the cap matches the other in-process caches.
_COMMENT_AVATAR_CACHE: dict[str, tuple[float, str]] = {}
_COMMENT_AVATAR_CACHE_TTL = 24 * 3600
_COMMENT_AVATAR_MISS_TTL = 10 * 60
_COMMENT_AVATAR_CACHE_CAP = 2000


def _comment_avatar_cached(author_id: str) -> tuple[bool, str]:
    entry = _COMMENT_AVATAR_CACHE.get(author_id)
    if not entry:
        return False, ""
    stored_at, url = entry
    ttl = _COMMENT_AVATAR_CACHE_TTL if url else _COMMENT_AVATAR_MISS_TTL
    if time.time() - stored_at > ttl:
        _COMMENT_AVATAR_CACHE.pop(author_id, None)
        return False, ""
    return True, url


def _comment_avatar_store(author_id: str, url: str) -> None:
    _COMMENT_AVATAR_CACHE[author_id] = (time.time(), url)
    while len(_COMMENT_AVATAR_CACHE) > _COMMENT_AVATAR_CACHE_CAP:
        _COMMENT_AVATAR_CACHE.pop(next(iter(_COMMENT_AVATAR_CACHE)))


def _fetch_commenter_avatar(author_id: str, base_url: str, timeout: float) -> str:
    """One commenter's avatar via their channel page, for the batch endpoint below."""
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            response = client.get(f"{base_url}/api/v1/channels/{author_id}")
        if response.status_code != 200:
            return ""
        payload = response.json()
        if not isinstance(payload, dict):
            return ""
        return _comment_avatar(payload.get("authorThumbnails"))
    except Exception as exc:  # noqa: BLE001 - a missing avatar stays as initials
        logger.info("comment avatar lookup failed for %s: %s", author_id, exc)
        return ""


def _download_filename(title: str, video_id: str, extension: str = "mp4") -> str:
    """A filename a file manager will accept, without leaking path separators.

    Shared with the job runner so a direct passthrough and a merged download cannot end
    up naming the same video differently.
    """
    return downloads.safe_filename(title, video_id, extension)


@app.get("/api/download/jobs", tags=["downloads"], summary="List download jobs")
def api_list_download_jobs(request: Request) -> dict[str, Any]:
    """The caller's download jobs, running or finished.

    Declared above /api/download/{youtube_video_id} on purpose: routes match in
    declaration order, so the parameterised one would otherwise capture "jobs" as a
    video id and answer 502.
    """
    user_key = _user_key(request)
    # list_jobs returns internal DownloadJob objects; public() intentionally omits
    # ownership. Filter before projecting, and never attribute ownerless jobs to local.
    return {
        "ok": True,
        "items": [
            job.public() for job in downloads.manager.list_jobs()
            if getattr(job, "user_key", None) == user_key
        ],
    }


@app.get("/api/download/{youtube_video_id}")
def api_download_video(request: Request, youtube_video_id: str, itag: str = ""):
    """Stream one progressive rendition back as a file download.

    Only muxed streams are offered: the high-resolution DASH renditions are separate
    audio and video tracks and would need remuxing before they are useful as a file.
    `itag` picks a specific rendition from /api/download/{id}/formats; without it the
    best muxed stream is used, which is what the button did before the picker existed.
    """
    auth_redirect = _login_required_redirect(request)
    if auth_redirect:
        return auth_redirect
    try:
        playback = playback_for_video(youtube_video_id, itag.strip())
    except PlaybackError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        logger.warning("download lookup failed for %s: %s", youtube_video_id, exc)
        raise HTTPException(status_code=502, detail="Could not reach the playback backend") from exc

    stream_url = str(playback.get("stream_url") or "")
    if not stream_url:
        raise HTTPException(status_code=502, detail="No downloadable stream is available for this video")

    video = db.get_video_by_youtube_id(youtube_video_id) or {}
    title = str(video.get("title") or playback.get("title") or youtube_video_id)
    stream_type = str(playback.get("stream_type") or "video/mp4")
    extension = "webm" if "webm" in stream_type else "mp4"
    filename = _download_filename(title, youtube_video_id, extension)

    client = _invidious_client()
    # Followed here for the same reason as the yt-dlp stream: a 302 handed back to the
    # browser would quietly turn a proxied download into a direct one.
    upstream = client.send(
        client.build_request("GET", stream_url, headers={"user-agent": "Mozilla/5.0"}),
        stream=True,
        follow_redirects=True,
    )
    if upstream.status_code >= 400:
        upstream.close()
        raise HTTPException(status_code=502, detail=f"The stream host returned HTTP {upstream.status_code}")

    headers = {
        "content-disposition": f'attachment; filename="{filename}"; filename*=UTF-8\'\'{urllib.parse.quote(filename)}',
        "content-type": stream_type.split(";")[0] or "video/mp4",
        "cache-control": "no-store",
    }
    if upstream.headers.get("content-length"):
        headers["content-length"] = upstream.headers["content-length"]
    return StreamingResponse(
        upstream.iter_bytes(),
        status_code=200,
        headers=headers,
        background=BackgroundTask(upstream.close),
    )


@app.get("/api/preview/{youtube_video_id}")
def api_preview_video(request: Request, youtube_video_id: str):
    """Proxied bytes for hover previews: the lightest muxed rendition.

    The browser cannot use Invidious's signed stream URLs directly (they are
    bound to this server's address), so the bytes are proxied same-origin,
    exactly like the yt-dlp fallback player. Resolution prefers the cached
    enrichment metadata so a hover rarely costs an Invidious round trip; when
    the cached signature has expired the metadata is refreshed once and the
    upstream retried before giving up. Anything unplayable is a 404 and the
    card simply keeps its thumbnail.
    """
    auth_redirect = _login_required_redirect(request)
    if auth_redirect:
        return auth_redirect
    if not VIDEO_ID_RE.match(youtube_video_id or ""):
        raise HTTPException(status_code=404, detail="Unknown video")

    raw = _cached_preview_raw(youtube_video_id)
    if raw is None:
        raw = _refresh_preview_metadata(youtube_video_id)
    stream = select_preview_stream(raw or {})
    if stream is None:
        raise HTTPException(status_code=404, detail="No preview is available for this video")

    upstream = _fetch_preview_upstream(str(stream.get("url") or ""), request.headers.get("range"))
    if upstream is not None and upstream.status_code >= 400:
        upstream.close()
        upstream = None
        refreshed = _refresh_preview_metadata(youtube_video_id)
        stream = select_preview_stream(refreshed or {})
        if stream is None:
            raise HTTPException(status_code=404, detail="No preview is available for this video")
        upstream = _fetch_preview_upstream(str(stream.get("url") or ""), request.headers.get("range"))
    if upstream is None:
        raise HTTPException(status_code=502, detail="Could not reach the playback backend")
    if upstream.status_code >= 400:
        upstream.close()
        raise HTTPException(status_code=404, detail="No preview is available for this video")
    return _preview_response(upstream, stream)


def _cached_preview_raw(youtube_video_id: str) -> dict[str, Any] | None:
    """Full Invidious payload from a past enrichment, if one was stored.

    Enrichment unwraps the payload one level when it stores it, so most rows
    carry the Invidious response directly while a few keep it under `raw`;
    both shapes are accepted, mirroring _tags_from_metadata.
    """
    try:
        video = db.get_video_by_youtube_id(youtube_video_id) or {}
    except Exception as exc:  # noqa: BLE001 - a preview never breaks on a DB hiccup
        logger.info("preview cache lookup failed for %s: %s", youtube_video_id, exc)
        return None
    metadata = video.get("metadata")
    if not isinstance(metadata, dict):
        return None
    raw = metadata.get("raw")
    if not isinstance(raw, dict):
        raw = metadata
    return raw


def _refresh_preview_metadata(youtube_video_id: str) -> dict[str, Any] | None:
    """Fetch and store fresh Invidious metadata; doubles as enrichment backfill."""
    try:
        result = enrich_video(youtube_video_id)
    except Exception as exc:  # noqa: BLE001 - unplayable is a 404, not a 500
        logger.info("preview metadata refresh failed for %s: %s", youtube_video_id, exc)
        return None
    if not result.get("ok"):
        return None
    return _cached_preview_raw(youtube_video_id)


def _fetch_preview_upstream(stream_url: str, byte_range: str | None):
    if not stream_url:
        return None
    upstream_headers = {"user-agent": "Mozilla/5.0"}
    if byte_range:
        upstream_headers["range"] = byte_range
    client = _invidious_client()
    try:
        # A 302 handed back would quietly turn a proxied preview into a direct
        # one, leaking the viewer address this proxy exists to keep private.
        return client.send(
            client.build_request("GET", stream_url, headers=upstream_headers),
            stream=True,
            follow_redirects=True,
        )
    except Exception as exc:  # noqa: BLE001 - unplayable is a 404, not a 500
        logger.info("preview upstream fetch failed: %s", exc)
        return None


def _preview_response(upstream, stream: dict[str, Any]):
    headers = {"accept-ranges": "bytes"}
    for name in ("content-length", "content-range", "content-type"):
        if upstream.headers.get(name):
            headers[name] = upstream.headers[name]
    headers.setdefault("content-type", str(stream.get("type") or "video/mp4").split(";")[0])
    # Full responses are addressable per video id, so a re-hover replays from the
    # browser cache instead of proxying the opening bytes again.
    headers["cache-control"] = "private, max-age=300" if upstream.status_code == 200 else "no-store"
    return StreamingResponse(
        upstream.iter_bytes(),
        status_code=upstream.status_code,
        headers=headers,
        background=BackgroundTask(upstream.close),
    )


@app.get("/api/download/{youtube_video_id}/info")
def api_download_info(request: Request, youtube_video_id: str) -> dict[str, Any]:
    """What the download button would give you, so the UI can say so before starting."""
    auth_redirect = _login_required_redirect(request)
    if auth_redirect:
        raise HTTPException(status_code=401, detail="Login required")
    try:
        playback = playback_for_video(youtube_video_id)
    except Exception as exc:
        return {"ok": False, "detail": str(exc)}
    return {
        "ok": True,
        "quality": playback.get("quality") or "",
        "stream_type": playback.get("stream_type") or "",
        "filename": _download_filename(str(playback.get("title") or youtube_video_id), youtube_video_id),
    }


@app.get("/api/download/{youtube_video_id}/formats")
def api_download_formats(request: Request, youtube_video_id: str) -> dict[str, Any]:
    """Every rendition the picker can offer, from both backends.

    Invidious supplies the muxed streams it can hand over directly — a plain HTTP
    passthrough, no processing. yt-dlp supplies everything above that, which YouTube
    publishes as separate video and audio tracks and which therefore has to be fetched
    and muxed as a background job. Both kinds appear in one list, tagged so the UI can
    say which is instant and which needs work.
    """
    auth_redirect = _login_required_redirect(request)
    if auth_redirect:
        raise HTTPException(status_code=401, detail="Login required")

    formats: list[dict[str, Any]] = []
    title = ""
    invidious_error = ""
    try:
        options = download_options(youtube_video_id)
        title = str(options.get("title") or "")
        for item in options.get("formats") or []:
            container = item.get("container") or "mp4"
            formats.append({
                "mode": "direct",
                "height": item.get("height") or 0,
                "label_height": item.get("height") or 0,
                "quality": item.get("quality") or "",
                "container": container,
                "size_bytes": item.get("size_bytes"),
                "itag": item.get("itag") or "",
                "needs_merge": False,
                "filename": _download_filename(title or youtube_video_id, youtube_video_id, container),
            })
    except Exception as exc:
        invidious_error = str(exc)
        logger.info("invidious had no download formats for %s: %s", youtube_video_id, exc)

    ytdlp_error = ""
    if ytdlp.available():
        try:
            extra = ytdlp.download_formats(youtube_video_id)
            title = title or str(extra.get("title") or "")
            known = {item["height"] for item in formats if item["height"]}
            for item in extra.get("formats") or []:
                # Compare on the label, not the pixel height: Invidious reports "360p" as
                # 360, while yt-dlp's height for that same portrait rendition is 640.
                label_height = item.get("label_height") or 0
                if label_height and label_height in known:
                    continue
                container = "mp4" if item.get("ext") == "mp4" else "mkv"
                formats.append({
                    "mode": "merge" if item.get("needs_merge") else "ytdlp",
                    # `height` drives the format selector and must stay the real one.
                    "height": item.get("height") or 0,
                    "label_height": label_height,
                    "quality": item.get("quality") or (f"{label_height}p" if label_height else ""),
                    "container": container,
                    "size_bytes": item.get("size_bytes"),
                    "size_estimated": bool(item.get("size_estimated")),
                    "itag": "",
                    "needs_merge": bool(item.get("needs_merge")),
                    "filename": _download_filename(title or youtube_video_id, youtube_video_id, container),
                })
        except Exception as exc:
            ytdlp_error = str(exc)
            logger.warning("yt-dlp had no download formats for %s: %s", youtube_video_id, exc)

    formats.sort(key=lambda item: (item["label_height"] or 0, item["mode"] == "direct"), reverse=True)
    detail = ""
    if not formats:
        detail = invidious_error or ytdlp_error or "No downloadable stream is available for this video"
    return {
        "ok": bool(formats),
        "title": title or youtube_video_id,
        "formats": formats,
        "ytdlp": ytdlp.available(),
        "ffmpeg": ytdlp.ffmpeg_available(),
        "detail": detail,
    }


@app.post("/api/download/{youtube_video_id}/jobs")
def api_start_download_job(request: Request, youtube_video_id: str, payload: DownloadJobRequest) -> dict[str, Any]:
    """Kick off a merged download. Returns immediately with a job to poll."""
    auth_redirect = _login_required_redirect(request)
    if auth_redirect:
        raise HTTPException(status_code=401, detail="Login required")
    if not ytdlp.available():
        raise HTTPException(status_code=503, detail="yt-dlp is not available in this deployment")
    if payload.needs_merge and not ytdlp.ffmpeg_available():
        raise HTTPException(status_code=503, detail="ffmpeg is required to merge video and audio, and is not installed")

    video = db.get_video_by_youtube_id(youtube_video_id) or {}
    title = str(payload.title or video.get("title") or youtube_video_id)
    height = max(0, min(int(payload.height or 0), get_settings().download_max_height))
    try:
        job = downloads.manager.start(
            youtube_video_id,
            height,
            payload.container,
            title,
            destination=payload.destination,
            user_key=_user_key(request),
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    return {"ok": True, "job": job.public()}


def _owned_download_job(job_id: str, user_key: str) -> downloads.DownloadJob:
    job = downloads.manager.get(job_id)
    # Ownerless legacy jobs cannot be attributed safely, even in local mode. Jobs
    # explicitly owned by "local" still work for the single-user browser installation.
    if not job or getattr(job, "user_key", None) != user_key:
        raise HTTPException(status_code=404, detail="No such download job")
    return job


@app.get("/api/download/jobs/{job_id}")
def api_download_job(request: Request, job_id: str) -> dict[str, Any]:
    """Progress for one download job."""
    auth_redirect = _login_required_redirect(request)
    if auth_redirect:
        raise HTTPException(status_code=401, detail="Login required")
    job = _owned_download_job(job_id, _user_key(request))
    return {"ok": True, "job": job.public()}


@app.delete("/api/download/jobs/{job_id}")
def api_cancel_download_job(request: Request, job_id: str) -> dict[str, Any]:
    auth_redirect = _login_required_redirect(request)
    if auth_redirect:
        raise HTTPException(status_code=401, detail="Login required")
    _owned_download_job(job_id, _user_key(request))
    return {"ok": True, "cancelled": downloads.manager.cancel(job_id)}


@app.get("/api/download/jobs/{job_id}/file")
def api_download_job_file(request: Request, job_id: str):
    """Hand over the finished file."""
    auth_redirect = _login_required_redirect(request)
    if auth_redirect:
        return auth_redirect
    job = _owned_download_job(job_id, _user_key(request))
    if job.status != "done":
        raise HTTPException(status_code=409, detail=f"Download is {job.status}, not finished")
    path = downloads.resolve_completed_path(job)
    if not path:
        raise HTTPException(status_code=410, detail="The finished file is no longer on disk")
    filename = job.filename or path.name
    return FileResponse(
        path,
        media_type="video/mp4" if path.suffix == ".mp4" else "application/octet-stream",
        filename=filename,
        headers={"cache-control": "no-store"},
    )


def _comment_identity(raw: dict[str, Any]) -> str:
    """A stable id for one comment.

    Invidious does not hand back YouTube's comment id, so it is derived from the fields
    that identify a comment and do not change between fetches. Hashed rather than stored
    raw so the key stays a fixed length and holds no comment text.
    """
    seed = "|".join([
        str(raw.get("authorId") or raw.get("author") or ""),
        str(raw.get("published") or raw.get("publishedText") or ""),
        str(raw.get("content") or "")[:280],
    ])
    return hashlib.sha1(seed.encode("utf-8", "replace")).hexdigest()[:24]


@app.post("/api/comments/reaction")
def api_comment_reaction(request: Request, payload: CommentReactionRequest) -> dict[str, Any]:
    """Record a local vote on a comment, and a small hint about the video it sits under.

    Nothing is sent to YouTube — Invidious can read comments but cannot vote on them —
    so this is stored here and used only for the button state and for ranking.
    """
    auth_redirect = _login_required_redirect(request)
    if auth_redirect:
        raise HTTPException(status_code=401, detail="Login required")
    video_id = payload.youtube_video_id.strip()
    comment_id = payload.comment_id.strip()
    if not video_id or not comment_id:
        raise HTTPException(status_code=400, detail="A video id and comment id are required")

    user_key = _user_key(request)
    reaction = db.set_comment_reaction(video_id, comment_id, payload.reaction.strip(), user_key)
    if reaction:
        db.record_video_event(
            video_id,
            f"comment_{reaction}",
            {"source": "comment_vote", "comment_id": comment_id},
            user_key,
        )
    return {"ok": True, "reaction": reaction}


def _download_file_path(record: dict[str, Any]) -> Path | None:
    """The stored file, but only if it really sits inside the downloads folder."""
    raw = str(record.get("path") or "")
    if not raw:
        return None
    directory = Path(get_settings().download_dir).resolve()
    try:
        path = Path(raw).resolve()
        path.relative_to(directory)
    except (OSError, ValueError):
        return None
    return path if path.is_file() else None


@app.get("/api/downloads")
def api_list_downloads(request: Request, q: str = "", limit: int = 200) -> dict[str, Any]:
    auth_redirect = _login_required_redirect(request)
    if auth_redirect:
        raise HTTPException(status_code=401, detail="Login required")
    rows = db.list_downloads(_user_key(request), q, limit)
    return {"ok": True, "items": _json_ready(rows), "count": len(rows)}


@app.get("/api/downloads/{youtube_video_id}/file")
def api_download_file(request: Request, youtube_video_id: str):
    """Stream a kept download, with range support so the player can seek."""
    auth_redirect = _login_required_redirect(request)
    if auth_redirect:
        return auth_redirect
    record = db.get_download(youtube_video_id, _user_key(request))
    if not record:
        raise HTTPException(status_code=404, detail="Not downloaded")
    path = _download_file_path(record)
    if not path:
        raise HTTPException(status_code=410, detail="The downloaded file is no longer on disk")
    media_type = "video/mp4" if path.suffix == ".mp4" else "video/x-matroska"
    # FileResponse handles Range itself, which is what makes seeking work.
    return FileResponse(path, media_type=media_type, filename=str(record.get("filename") or path.name))


@app.delete("/api/downloads/{youtube_video_id}")
def api_delete_download(request: Request, youtube_video_id: str) -> dict[str, Any]:
    auth_redirect = _login_required_redirect(request)
    if auth_redirect:
        raise HTTPException(status_code=401, detail="Login required")
    record = db.delete_download(youtube_video_id, _user_key(request))
    if not record:
        raise HTTPException(status_code=404, detail="Not downloaded")
    path = _download_file_path(record)
    if path:
        downloads.unlink_unreferenced_download(path)
    return {"ok": True, "youtube_video_id": youtube_video_id}


@app.get("/api/", tags=["system"], summary="API index")
def api_index(request: Request) -> dict[str, Any]:
    """What this API can do, and who you are to it.

    A single request that orients a new client without reading the whole OpenAPI document:
    it confirms the credential works, says whether it is read-only, and points at the
    routes for each capability.
    """
    user = _current_user(request)
    routes = sorted(
        {
            f"{method} {route.path}"
            for route in app.routes
            if getattr(route, "path", "").startswith("/api/")
            for method in getattr(route, "methods", set()) - {"HEAD", "OPTIONS"}
        }
    )
    return {
        "ok": True,
        "service": "OpenFeed",
        "version": __version__,
        "authenticated": bool(user.get("authenticated")),
        "via_token": bool(user.get("via_token")),
        "read_only": bool(user.get("token_read_only")),
        "is_admin": _user_is_admin(user),
        "user_key": str(user.get("user_key") or "local"),
        "documentation": {"swagger": "/docs", "redoc": "/redoc", "openapi": "/openapi.json"},
        "capabilities": {
            "feed": ["/api/home/sections", "/api/recommendations", "/api/shorts", "/api/search"],
            "videos": ["/api/videos", "/api/videos/{id}", "/api/transcript/{id}", "/api/feedback"],
            "playback": ["/api/playback/sources", "/api/playback/progress/{id}", "/api/playback/diagnose"],
            "downloads": ["/api/download/{id}/formats", "/api/download/{id}/jobs", "/api/downloads"],
            "subscriptions": ["/api/subscriptions", "/api/channels", "/api/subscription-collections"],
            "playlists": ["/api/playlists"],
            "preferences": ["/api/feed/preferences", "/api/playback/preferences"],
            "metadata": ["/api/enrich/recent", "/api/discover/related", "/api/ryd/refresh"],
            "tokens": ["/api/tokens"],
            "system": ["/api/status/full", "/api/sync/rss", "/healthz"],
        },
        "route_count": len(routes),
        "routes": routes,
    }


@app.post("/api/takeout/import", tags=["metadata"], summary="Import a YouTube Takeout archive")
async def api_takeout_import(request: Request, archive: UploadFile = File(...)) -> dict[str, Any]:
    """Parse a Takeout .zip and use it as personalization signal.

    Everything is processed in memory in this process. The archive is never written to
    disk and nothing leaves the server.
    """
    user_key = _user_key(request)
    data = await archive.read()
    if not data:
        raise HTTPException(status_code=400, detail="The upload was empty")
    try:
        parsed = takeout.inspect(data)
    except takeout.TakeoutError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.warning("takeout import failed: %s", exc)
        raise HTTPException(status_code=500, detail="Could not read that archive") from exc
    finally:
        # Watch history is about as sensitive as it gets; drop the bytes as soon as the
        # parse is done rather than leaving them for the garbage collector to get to.
        del data

    summary = takeout.summarize(parsed)
    record = db.store_takeout_import(parsed, summary, user_key)
    # Refresh immediately: an import that does not change the feed looks like it
    # failed, and the worker's next pass could be half an hour away.
    try:
        from . import topics

        db.clear_takeout_cache(user_key)
        # Seed from the channels the archive says they watch before re-ranking:
        # otherwise the import changes the weights but there is nothing new to weigh.
        discovery.grow_library(user_key)
        topics.clear_cache()
        topics.rebuild_vectors(1200)
        db.clear_watch_time_cache(user_key)
    except Exception as exc:  # noqa: BLE001 - the import itself already succeeded
        logger.warning("post-import refresh failed: %s", exc)

    # Subscriptions are reported, never acted on: following hundreds of channels
    # silently because somebody uploaded a file is not a decision this endpoint
    # gets to make.
    return {
        "ok": True,
        "summary": summary,
        "imported_at": _json_ready_value(record.get("created_at")),
        "note": "Subscriptions were read for ranking only. Use Subscriptions to follow any of them here.",
    }


@app.get("/api/takeout", tags=["metadata"], summary="Imported Takeout status")
def api_takeout_status(request: Request) -> dict[str, Any]:
    user_key = _user_key(request)
    record = db.latest_takeout_import(user_key)
    profile = db.takeout_profile(user_key)
    return {
        "ok": True,
        "imported": bool(record),
        "imported_at": _json_ready_value((record or {}).get("created_at")),
        "summary": _json_ready_value((record or {}).get("summary") or {}),
        "channels_tracked": len(profile["channels"]),
        "watched_videos": profile["watched_videos"],
        "confidence": round(profile["confidence"], 3),
    }


@app.delete("/api/takeout", tags=["metadata"], summary="Delete imported Takeout data")
def api_takeout_delete(request: Request) -> dict[str, Any]:
    """Remove every row an import created. The feed reverts to its own observations."""
    return {"ok": True, "removed": db.clear_takeout_data(_user_key(request))}


@app.get("/api/tokens", tags=["tokens"], summary="List API tokens")
def api_list_tokens(request: Request, include_revoked: bool = False) -> dict[str, Any]:
    """Existing tokens. The secrets themselves are unrecoverable and never appear here."""
    user_key = _user_key(request)
    rows = db.list_api_tokens(user_key, include_revoked)
    return {"ok": True, "items": [_json_ready_value(tokens.public_view(row)) for row in rows]}


@app.post("/api/tokens", tags=["tokens"], summary="Create an API token", status_code=201)
def api_create_token(request: Request, payload: ApiTokenRequest) -> dict[str, Any]:
    """Mint a token with the browser issuer's admin capability snapshotted server-side.

    The plaintext is returned **once** and cannot be retrieved again.
    """
    user_key = _user_key(request)
    plaintext, prefix, token_hash = tokens.generate()
    row = db.create_api_token(
        payload.name, prefix, token_hash, payload.read_only, user_key,
        delegated_admin=_user_is_admin(_current_user(request)),
    )
    return {
        "ok": True,
        "token": plaintext,
        "warning": "Copy this now — it is stored only as a hash and cannot be shown again.",
        "item": _json_ready_value(tokens.public_view(row)),
    }


@app.delete("/api/tokens/{token_id}", tags=["tokens"], summary="Revoke an API token")
def api_revoke_token(request: Request, token_id: str) -> dict[str, Any]:
    user_key = _user_key(request)
    if not _valid_uuid(token_id):
        raise HTTPException(status_code=404, detail="No such token")
    row = db.revoke_api_token(token_id, user_key)
    if not row:
        raise HTTPException(status_code=404, detail="No such token")
    return {"ok": True, "item": _json_ready_value(tokens.public_view(row))}


@app.get("/api/videos/{youtube_video_id}", tags=["videos"], summary="Get one video")
def api_get_video(request: Request, youtube_video_id: str) -> dict[str, Any]:
    """Everything known about a single video, including this user's state for it."""
    user_key = _user_key(request)
    video = db.get_video_by_youtube_id(youtube_video_id)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    return {
        "ok": True,
        "video": _json_ready_value(video),
        "feedback": _json_ready_value(db.get_video_feedback(youtube_video_id, user_key) or {}),
        "progress": _json_ready_value(db.get_playback_progress(youtube_video_id, user_key) or {}),
        "download": _json_ready_value(db.get_download(youtube_video_id, user_key) or {}),
        "subscribed": bool(_subscription_feed_for_video(video)),
    }


@app.get("/api/playback/progress/{youtube_video_id}", tags=["playback"], summary="Get playback progress")
def api_get_playback_progress(request: Request, youtube_video_id: str) -> dict[str, Any]:
    """Where this video was left off. POST to the same area records it."""
    progress = db.get_playback_progress(youtube_video_id, _user_key(request))
    return {"ok": True, "progress": _json_ready_value(progress or {}), "found": bool(progress)}


@app.post("/api/ryd/refresh", tags=["metadata"], summary="Fetch missing dislike estimates")
def api_ryd_refresh(request: Request, limit: int = 40) -> dict[str, Any]:
    """Run one Return YouTube Dislike batch. A no-op unless the feature is enabled."""
    user_key = _user_key(request)
    return {"ok": True, "result": ryd.refresh_missing(max(1, min(limit, 200)), user_key)}


@app.get("/api/transcript/{youtube_video_id}", tags=["videos"], summary="Get a transcript")
def api_transcript(request: Request, youtube_video_id: str) -> dict[str, Any]:
    """Stored transcript segments, when enrichment captured any."""
    _user_key(request)
    video = db.get_video_by_youtube_id(youtube_video_id)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    metadata = video.get("metadata") if isinstance(video.get("metadata"), dict) else {}
    segments = metadata.get("transcript_segments") if isinstance(metadata, dict) else None
    segments = segments if isinstance(segments, list) else []
    return {"ok": True, "youtube_video_id": youtube_video_id, "segments": _json_ready(segments), "count": len(segments)}


@app.get("/api/videos/{youtube_video_id}/dislikes")
def api_video_dislikes(request: Request, youtube_video_id: str) -> dict[str, Any]:
    """The stored dislike estimate, if one has arrived.

    Read-only against our own database — polling this never reaches RYD, so a page left
    open cannot turn into repeated outbound requests.
    """
    auth_redirect = _login_required_redirect(request)
    if auth_redirect:
        raise HTTPException(status_code=401, detail="Login required")
    state = db.video_dislike_state(youtube_video_id)
    count = state.get("dislike_count")
    return {
        "ok": True,
        "youtube_video_id": youtube_video_id,
        "dislikes": count,
        "display": _compact_count(count) if count is not None else "",
        "estimated": state.get("dislike_source") == "ryd",
        "checked": bool(state.get("checked")),
    }


@app.get("/api/videos/{youtube_video_id}/details")
def api_video_details(request: Request, youtube_video_id: str) -> dict[str, Any]:
    """The watch page's description block, re-rendered from the stored row.

    A freshly opened page can render before background enrichment fills in the
    description, view count or publish date. The watch script polls this -- our
    own database only, never Invidious -- and swaps the block when it completes,
    so the first paint does not freeze "No description" on screen. Markup comes
    from the server for the same reason channel cards do: one renderer, no drift.
    """
    _user_key(request)
    video = db.get_video_by_youtube_id(youtube_video_id)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    summary_text = _description_text(video.get("summary") or video.get("description") or "")
    has_description = bool(summary_text.strip())
    views = _compact_count(video.get("view_count"))
    view_text = f"{escape(views)} views" if views else "Views unavailable"
    published_relative = _relative_time(video.get("published_at"))
    published_date = _date_text(video.get("published_at"))
    published_text = escape(published_relative or published_date)
    description_meta = " &middot; ".join(part for part in [view_text, published_text] if part)
    complete = (
        video.get("view_count") is not None
        and has_description
        and not _published_date_is_decoded(video.get("published_at"))
        and video.get("published_at") is not None
    )
    return {
        "ok": True,
        "youtube_video_id": youtube_video_id,
        "complete": complete,
        "has_description": has_description,
        "meta_html": description_meta,
        "body_html": f"<p>{_rich_text(summary_text)}</p>" if has_description else "",
    }


@app.get("/api/comments/{youtube_video_id}")
def api_comments(request: Request, youtube_video_id: str, continuation: str = "") -> dict[str, Any]:
    """Fetch comments from Invidious. Off unless the user opts in.

    With a continuation token this returns one thread's replies instead of the top-level
    comments — Invidious serves both from the same endpoint.
    """
    auth_redirect = _login_required_redirect(request)
    if auth_redirect:
        raise HTTPException(status_code=401, detail="Login required")
    preferences = db.get_feed_preferences(_user_key(request))
    if not preferences.get("show_comments"):
        raise HTTPException(status_code=403, detail="Comments are disabled in settings")
    settings = get_settings()
    if not settings.invidious_base_url:
        raise HTTPException(status_code=502, detail="Invidious is not configured")
    token = continuation.strip()
    try:
        with httpx.Client(timeout=max(5, settings.enrichment_timeout_seconds)) as client:
            response = client.get(
                f"{settings.invidious_base_url}/api/v1/comments/{youtube_video_id}",
                params={"continuation": token} if token else None,
            )
    except Exception as exc:
        logger.warning("comment fetch failed for %s: %s", youtube_video_id, exc)
        raise HTTPException(status_code=502, detail="Could not reach the comment source")
    if response.status_code != 200:
        raise HTTPException(status_code=502, detail="Comments are unavailable for this video")
    payload = response.json()
    try:
        stored_reactions = db.comment_reactions(youtube_video_id, _user_key(request))
    except Exception as exc:  # noqa: BLE001 - the votes are decorative, the comments are not
        logger.info("could not load comment reactions for %s: %s", youtube_video_id, exc)
        stored_reactions = {}
    items = []
    for raw in (payload.get("comments") or [])[: (COMMENT_REPLY_LIMIT if token else COMMENT_LIMIT)]:
        if not isinstance(raw, dict):
            continue
        replies = raw.get("replies") if isinstance(raw.get("replies"), dict) else {}
        # Invidious exposes no stable per-comment id, so one is derived from the parts
        # that do not change: who wrote it, when, and what it says.
        comment_id = _comment_identity(raw)
        items.append({
            "comment_id": comment_id,
            "reaction": stored_reactions.get(comment_id, ""),
            "author": str(raw.get("author") or "Unknown"),
            "author_id": str(raw.get("authorId") or ""),
            "avatar_url": _comment_avatar(raw.get("authorThumbnails")),
            "content": str(raw.get("content") or ""),
            "like_count": int(raw.get("likeCount") or 0),
            "published_text": str(raw.get("publishedText") or ""),
            "is_owner": bool(raw.get("authorIsChannelOwner")),
            "is_pinned": bool(raw.get("isPinned")),
            "verified": bool(raw.get("verified")),
            # Invidious surfaces the creator's heart, including whose it is.
            "creator_heart": _proxied_avatar((raw.get("creatorHeart") or {}).get("creatorThumbnail"))
                             if isinstance(raw.get("creatorHeart"), dict) else "",
            "creator_heart_name": str((raw.get("creatorHeart") or {}).get("creatorName") or "")
                                  if isinstance(raw.get("creatorHeart"), dict) else "",
            "reply_count": int(replies.get("replyCount") or 0),
            # Handed to the client so only the threads a reader opens cost a request.
            "reply_continuation": str(replies.get("continuation") or ""),
        })
    return {
        "ok": True,
        "youtube_video_id": youtube_video_id,
        "comment_count": payload.get("commentCount"),
        "items": items,
        "is_replies": bool(token),
        # Was dropped, so the client had no way to ask for page two — a video with 2,700
        # comments showed the first twenty and stopped.
        "continuation": str(payload.get("continuation") or ""),
    }


@app.post("/api/comments/avatars")
def api_comment_avatars(request: Request, payload: CommentAvatarsRequest) -> dict[str, Any]:
    """Author avatars for already-rendered comments, resolved lazily as one batch.

    The comments endpoint carries no avatar URLs (Invidious returns an empty
    authorThumbnails list for every comment), so rows render as initials first
    and the watch script asks here for the authors on screen. Resolved against
    the channel endpoint, which still carries thumbnails, with a bounded worker
    pool and per-fetch timeout so one slow lookup cannot hold the request open.
    Anything unresolved stays as initials.
    """
    auth_redirect = _login_required_redirect(request)
    if auth_redirect:
        raise HTTPException(status_code=401, detail="Login required")
    preferences = db.get_feed_preferences(_user_key(request))
    if not preferences.get("show_comments"):
        raise HTTPException(status_code=403, detail="Comments are disabled in settings")
    settings = get_settings()
    if not settings.invidious_base_url:
        raise HTTPException(status_code=502, detail="Invidious is not configured")
    seen: dict[str, None] = {}
    for raw in (payload.author_ids or [])[:25]:
        author_id = str(raw or "").strip()
        # Channel ids only: the value becomes an upstream path segment, so anything
        # else is dropped before it can reach a URL.
        if re.fullmatch(r"[A-Za-z0-9_-]{2,64}", author_id):
            seen[author_id] = None
    avatars: dict[str, str] = {}
    missing: list[str] = []
    for author_id in seen:
        hit, url = _comment_avatar_cached(author_id)
        if hit:
            if url:
                avatars[author_id] = url
        else:
            missing.append(author_id)
    missing = missing[:12]
    if missing:
        timeout = max(3.0, min(float(settings.enrichment_timeout_seconds or 12), 6.0))
        with ThreadPoolExecutor(max_workers=6) as pool:
            try:
                resolved = list(
                    pool.map(
                        lambda aid: _fetch_commenter_avatar(aid, settings.invidious_base_url, timeout),
                        missing,
                        timeout=max(10.0, timeout * 2),
                    )
                )
            except Exception:  # noqa: BLE001 - pool timeout still yields cached hits
                resolved = [""] * len(missing)
        for author_id, url in zip(missing, resolved):
            _comment_avatar_store(author_id, url)
            if url:
                avatars[author_id] = url
    return {"ok": True, "avatars": avatars}


@app.get("/api/sidebar/subscriptions", response_class=HTMLResponse)
def api_sidebar_subscriptions(request: Request) -> str:
    """The sidebar subscription list as HTML, so it can refresh in place after a
    subscribe or unsubscribe without a full page reload.

    This is the full list (limit=250), not the shell's VISIBLE + 1 probe: expanding
    "Show more" swaps this markup in, so the shell never has to fetch it upfront."""
    auth_redirect = _login_required_redirect(request)
    if auth_redirect:
        return ""
    return _sidebar_subscription_group(limit=250)


def _short_card(video: dict[str, Any], position: int) -> str:
    video_id = escape(str(video.get("youtube_video_id") or ""))
    title_text = str(video.get("title") or "Untitled")
    title = escape(_text_excerpt(title_text, 90))
    channel_text = str(video.get("channel_title") or video.get("author") or "Unknown channel")
    thumb = escape(_thumbnail_for(video))
    views = _compact_count(video.get("view_count"))
    meta = f"{views} views" if views else _relative_time(video.get("published_at"))
    image = (
        f'<img src="{thumb}" alt="" loading="lazy" decoding="async" onerror="this.remove()">'
        if thumb else '<span class="thumb-fallback">OpenFeed</span>'
    )
    return f'''
    <article class="short-card" data-video-position="{position}">
      <a class="short-thumb" href="/shorts?start={video_id}" aria-label="{title}">
        {image}
        <span class="short-badge">{_icon("shorts")}</span>
      </a>
      <div class="short-copy">
        <h3><a href="/shorts?start={video_id}">{title}</a></h3>
        <p>{escape(channel_text)}{f' &middot; {escape(meta)}' if meta else ''}</p>
      </div>
    </article>
    '''


def _shorts_shelf(videos: list[dict[str, Any]], section_id: str = "shorts") -> str:
    if not videos:
        return ""
    cards = "".join(_short_card(video, index) for index, video in enumerate(videos, start=1))
    heading_id = f"heading-{escape(section_id)}"
    return f'''
      <section class="section-block" data-feed-section="{escape(section_id)}" data-section-kind="shorts" aria-labelledby="{heading_id}">
        <div class="section-heading">
          <h2 id="{heading_id}">{_icon("shorts")}<span>Shorts</span></h2>
          <a class="text-action" href="/?filter=short-videos">{_icon("chevron-right")}<span>See all</span></a>
        </div>
        <div class="shorts-row">{cards}</div>
      </section>
    '''


def _notification_allows(row: dict[str, Any], level: str, preferred_topics: set[str]) -> bool:
    """Whether one upload survives its channel's bell setting.

    'personalized' is the only level that needs a judgement call: it keeps uploads whose
    tags overlap the topics you follow. With no followed topics there is nothing to
    narrow by, so it behaves as 'all' rather than silently muting the channel.
    """
    if level == "none":
        return False
    if level != "personalized" or not preferred_topics:
        return True
    tags = {str(tag).strip().lower() for tag in (row.get("tags") or []) if str(tag).strip()}
    return bool(tags & preferred_topics)


def _notification_items(user_key: str, limit: int = 24) -> tuple[list[dict[str, Any]], int]:
    """Recent subscription uploads plus how many arrived since the panel was last opened."""
    preferences = db.get_feed_preferences(user_key)
    include_shorts = not preferences.get("exclude_shorts", False)
    levels = db.channel_notification_levels(user_key)
    preferred_topics = {str(topic).strip().lower() for topic in (preferences.get("preferred_topics") or []) if str(topic).strip()}
    # Over-fetch so muted channels cannot starve the panel down to a handful of rows.
    fetch = limit * 3 if levels else limit
    rows = [
        row for row in db.list_subscription_uploads(fetch, user_key, include_shorts)
        if _notification_allows(row, levels.get(str(row.get("youtube_channel_id") or ""), "all"), preferred_topics)
    ][:limit]
    seen_raw = db.get_notifications_seen_at(user_key)
    seen_at = None
    if seen_raw:
        try:
            seen_at = datetime.fromisoformat(str(seen_raw))
        except ValueError:
            seen_at = None
    unread = 0
    items: list[dict[str, Any]] = []
    for row in rows:
        published = row.get("published_at")
        is_new = bool(seen_at and published and published > seen_at) or (seen_at is None and published is not None)
        if is_new:
            unread += 1
        items.append({
            "youtube_video_id": str(row.get("youtube_video_id") or ""),
            "title": str(row.get("title") or "Untitled"),
            "channel_title": str(row.get("channel_title") or "Unknown channel"),
            "channel_avatar_url": row.get("channel_avatar_url") or "",
            "youtube_channel_id": str(row.get("youtube_channel_id") or ""),
            "thumbnail_url": _thumbnail_for(row),
            "published_text": _relative_time(published),
            "duration_text": _duration_text(row.get("duration_seconds")),
            "unread": is_new,
        })
    return items, unread


@app.get("/api/notifications")
def api_notifications(request: Request, limit: int = 24) -> dict[str, Any]:
    auth_redirect = _login_required_redirect(request)
    if auth_redirect:
        raise HTTPException(status_code=401, detail="Login required")
    items, unread = _notification_items(_user_key(request), max(1, min(limit, 60)))
    return {"ok": True, "items": items, "unread": unread}


@app.post("/api/notifications/seen")
def api_notifications_seen(request: Request) -> dict[str, Any]:
    auth_redirect = _login_required_redirect(request)
    if auth_redirect:
        raise HTTPException(status_code=401, detail="Login required")
    stamp = datetime.now(timezone.utc).isoformat()
    db.set_notifications_seen_at(stamp, _user_key(request))
    return {"ok": True, "seen_at": stamp}


@app.post("/api/channels/{youtube_channel_id}/notifications")
def api_set_channel_notifications(
    request: Request,
    youtube_channel_id: str,
    payload: ChannelNotificationRequest,
) -> dict[str, Any]:
    """Set the bell level for one channel: all, personalized, or none."""
    auth_redirect = _login_required_redirect(request)
    if auth_redirect:
        raise HTTPException(status_code=401, detail="Login required")
    level = payload.level.strip().lower()
    if level not in db.NOTIFICATION_LEVELS:
        raise HTTPException(status_code=400, detail="level must be all, personalized, or none")
    stored = db.set_channel_notification(youtube_channel_id.strip(), level, _user_key(request))
    return {"ok": True, "youtube_channel_id": youtube_channel_id, "level": stored}


def _shorts_slide(video: dict[str, Any], user_key: str = "local") -> str:
    video_id = escape(str(video.get("youtube_video_id") or ""))
    title_text = str(video.get("title") or "Untitled")
    title = escape(_text_excerpt(title_text, 140))
    channel_text = str(video.get("channel_title") or video.get("author") or "Unknown channel")
    channel_id = escape(str(video.get("youtube_channel_id") or ""))
    channel_href = f"/channel/{channel_id}" if channel_id else "/channels"
    thumb = escape(_thumbnail_for(video))
    views = _compact_count(video.get("view_count"))
    published = _relative_time(video.get("published_at"))
    meta = " · ".join(part for part in (f"{views} views" if views else "", published) if part)
    reaction = str(video.get("reaction") or "")
    like_selected = " is-selected" if reaction == "like" else ""
    dislike_selected = " is-selected" if reaction == "dislike" else ""
    poster = (
        f'<img class="shorts-poster" src="{thumb}" alt="" loading="lazy" decoding="async" onerror="this.remove()">'
        if thumb else ""
    )
    return f'''
    <article class="shorts-slide" data-shorts-slide data-video-id="{video_id}" data-video-card data-title="{escape(_text_excerpt(title_text, 90), quote=True)}">
      <div class="shorts-stage-inner">
        <div class="shorts-frame" data-shorts-frame>
          {poster}
          <button class="shorts-play" type="button" data-shorts-play aria-label="Play {title}">{_icon("play")}</button>
        </div>
        <div class="shorts-overlay">
          <a class="shorts-channel" href="{channel_href}">
            {_avatar_html(channel_text, video.get("channel_avatar_url"))}
            <span>{escape(channel_text)}</span>
          </a>
          <h2 class="shorts-title"><a href="/watch/{video_id}">{title}</a></h2>
          <p class="shorts-meta">{escape(meta)}</p>
        </div>
      </div>
      <div class="shorts-rail" role="group" aria-label="Actions for {title}">
        <button class="shorts-action{like_selected}" type="button" data-feedback="like" data-video-id="{video_id}" aria-pressed="{'true' if reaction == 'like' else 'false'}" aria-label="Like">{_icon("like")}<span>Like</span></button>
        <button class="shorts-action{dislike_selected}" type="button" data-feedback="dislike" data-video-id="{video_id}" aria-pressed="{'true' if reaction == 'dislike' else 'false'}" aria-label="Dislike">{_icon("dislike")}<span>Dislike</span></button>
        <button class="shorts-action" type="button" data-shorts-comments="{video_id}" aria-label="Comments">{_icon("caption")}<span>Comments</span></button>
        <button class="shorts-action" type="button" data-save-to-playlist="{video_id}" data-video-title="{escape(_text_excerpt(title_text, 90), quote=True)}" aria-label="Save to playlist">{_icon("playlist")}<span>Save</span></button>
        <button class="shorts-action" type="button" data-copy-url="/watch/{video_id}" aria-label="Share">{_icon("share")}<span>Share</span></button>
        <a class="shorts-action" href="/watch/{video_id}" aria-label="Open full watch page">{_icon("external")}<span>Open</span></a>
      </div>
    </article>
    '''


def _shorts_page_html(videos: list[dict[str, Any]], user_key: str) -> str:
    slides = "".join(_shorts_slide(video, user_key) for video in videos)
    if not slides:
        slides = '<p class="empty-copy">No Shorts in your library yet. Sync now, or turn Shorts on in Settings.</p>'
    return f'''
      <div class="shorts-viewport" data-shorts-viewport>
        <div class="shorts-stage" data-shorts-stage data-next-offset="{len(videos)}">
          {slides}
        </div>
        <div class="shorts-nav-buttons">
          <button class="shorts-nav" type="button" data-shorts-prev aria-label="Previous Short">{_icon("chevron-up")}</button>
          <button class="shorts-nav" type="button" data-shorts-next aria-label="Next Short">{_icon("chevron-down")}</button>
        </div>
        <aside class="shorts-comments" data-shorts-comment-panel hidden aria-label="Comments">
          <div class="shorts-comments-head">
            <h2>Comments</h2>
            <button class="icon-button" type="button" data-shorts-comments-close aria-label="Close comments">{_icon("x")}</button>
          </div>
          <div class="shorts-comments-body" data-shorts-comment-body></div>
        </aside>
      </div>
    '''


@app.get("/shorts", response_class=HTMLResponse)
def shorts_page(request: Request, start: str = ""):
    auth_redirect = _login_required_redirect(request)
    if auth_redirect:
        return auth_redirect
    user_key = _user_key(request)
    videos = db.list_short_videos(12, user_key, 0)
    if start:
        # Lead with the requested Short, keeping the rest of the feed behind it. It may sit
        # outside the first page of results, so look it up directly rather than only
        # searching what we already fetched.
        rest = [video for video in videos if str(video.get("youtube_video_id")) != start]
        leading = next((video for video in videos if str(video.get("youtube_video_id")) == start), None)
        if leading is None:
            leading = db.get_video_by_youtube_id(start)
        if leading is None:
            # Not in the library (for example opened straight from a channel page), so the
            # feed cannot show it — send the user to the page that can.
            return RedirectResponse(url=f"/watch/{start}", status_code=307)
        videos = [leading] + rest
    return _app_shell("Shorts", "shorts", _shorts_page_html(videos, user_key), main_class="shorts-main", request=request, immersive=True)


@app.get("/api/shorts")
def api_shorts(request: Request, offset: int = 0, limit: int = 8) -> dict[str, Any]:
    auth_redirect = _login_required_redirect(request)
    if auth_redirect:
        raise HTTPException(status_code=401, detail="Login required")
    user_key = _user_key(request)
    videos = db.list_short_videos(max(1, min(limit, 24)), user_key, max(0, offset))
    return {
        "ok": True,
        "html": "".join(_shorts_slide(video, user_key) for video in videos),
        "count": len(videos),
        "next_offset": max(0, offset) + len(videos),
    }


@app.post("/api/sync/rss")
def api_sync_rss() -> dict[str, Any]:
    try:
        result = sync_rss_once(get_settings().rss_poll_limit)
    except Exception as exc:  # noqa: BLE001 - surfacing as 502, never a 500
        logger.exception("RSS sync failed: %s", exc)
        raise HTTPException(status_code=502, detail=f"RSS sync failed: {exc}") from exc
    if not result.get("ok"):
        raise HTTPException(status_code=502, detail=result)
    return result


@app.get("/feeds/recommended.xml")
def recommended_feed() -> Response:
    settings = get_settings()
    videos = db.list_recommended_videos(50)
    now = _rss_date(datetime.now(timezone.utc))
    items = []
    for video in videos:
        video_id = video.get("youtube_video_id")
        title = escape(video.get("title") or str(video_id))
        link = f"{settings.public_url}/watch/{video_id}" if video_id else (video.get("url") or settings.public_url)
        published = video.get("published_at")
        pub_date = _rss_date(published)
        author = escape(video.get("channel_title") or video.get("author") or "Unknown channel")
        items.append(
            f"""
            <item>
              <guid isPermaLink="false">{escape(settings.rss_guid_namespace)}:video:{escape(str(video_id))}</guid>
              <title>{title}</title>
              <link>{escape(link)}</link>
              <pubDate>{escape(pub_date)}</pubDate>
              <description>{escape("Invidious discovery" if video.get("source_kind") == "discovery" else "Subscription")}: {author}</description>
            </item>
            """
        )
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0">
      <channel>
        <title>OpenFeed Recommendations</title>
        <link>{escape(settings.public_url)}</link>
        <description>OpenFeed recommendations ranked by feedback and recency. Retrieval and semantic scoring will expand this in later sprints.</description>
        <lastBuildDate>{escape(now)}</lastBuildDate>
        {''.join(items)}
      </channel>
    </rss>
    """
    return Response(content=xml, media_type="application/rss+xml")
