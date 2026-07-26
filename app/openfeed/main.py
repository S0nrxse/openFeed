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
from datetime import datetime, timezone
from email.utils import format_datetime
from html import escape, unescape
from typing import Any
from uuid import UUID

import httpx
from fastapi import FastAPI, HTTPException, Request, status
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
import redis

from . import __version__, db, gorse_client
from .config import get_settings
from .discovery import discover_related_videos
from .freshrss import FreshRSSClient
from .enrichment import enrich_channels, enrich_recent_videos, enrich_video
from .playback import PlaybackError, playback_for_video
from .sync import sync_freshrss_once


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="OpenFeed", version=__version__, lifespan=lifespan)

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


def _current_user(request: Request | None) -> dict[str, Any]:
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
    """Admin gating needs both a login to identify people and a group to check against.

    With either missing there is no identity to test, so everyone counts as admin -
    that keeps single-user installs working instead of locking the owner out of their
    own status page.
    """
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

    Static assets, /healthz and the /auth handshake sit outside these prefixes and are
    untouched; the Invidious proxies under /api/v1 keep their own guard as well.
    """
    path = request.url.path
    if not path.startswith(("/api/", "/admin/")):
        return await call_next(request)

    settings = get_settings()
    user = _current_user(request)
    wants_json = path.startswith("/api/")

    if settings.require_login and not user.get("authenticated"):
        if wants_json:
            return JSONResponse({"detail": "login_required"}, status_code=status.HTTP_401_UNAUTHORIZED)
        return _login_required_redirect(request) or _access_denied_response()
    if user.get("authenticated") and not _user_has_required_access(user):
        if wants_json:
            return JSONResponse({"detail": "openfeed_access_denied"}, status_code=status.HTTP_403_FORBIDDEN)
        return _access_denied_response()
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
    content = f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{escape(title)} - OpenFeed</title><style>body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#0f0f10;color:#f1f1f1;font-family:Inter,system-ui,sans-serif}}main{{width:min(560px,calc(100vw - 32px));border:1px solid rgba(255,255,255,.09);border-radius:18px;background:#181818;padding:24px}}h1{{margin:0 0 10px;font-size:24px}}p{{color:#a8a8a8;line-height:1.5}}code{{background:#242424;border-radius:6px;padding:2px 6px}}a{{color:#ff4d67}}</style></head><body><main><h1>{escape(title)}</h1><p>{escape(detail)}</p>{missing_html}<p><a href="/">Return to OpenFeed</a></p></main></body></html>'''
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


def _sync_gorse_event(user_key: str, youtube_video_id: str, event_type: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    video = db.get_video_by_youtube_id(youtube_video_id)
    if video:
        video = dict(video)
        video["topic_tokens"] = db._recommendation_tokens(video)
    return gorse_client.sync_feedback_event(user_key, video, event_type, payload or {})


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
}


class FeedbackRequest(BaseModel):
    youtube_video_id: str
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)


class SubscribeRequest(BaseModel):
    youtube_channel_id: str = ""
    title: str = ""
    feed_url: str = ""


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
    preferred_topics: list[str] = Field(default_factory=list)
    muted_topics: list[str] = Field(default_factory=list)
    prioritize_subscriptions: bool = True
    exclude_shorts: bool = False
    discovery_level: float = 0.35
    show_comments: bool = False


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
        return {"ok": bool(client.ping())}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


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


def _check_gorse() -> dict[str, Any]:
    settings = get_settings()
    if not settings.gorse_base_url:
        return {"ok": False, "configured": False}
    headers = {}
    if settings.gorse_server_api_key:
        headers["X-API-Key"] = settings.gorse_server_api_key
    with httpx.Client(timeout=5.0, follow_redirects=False) as client:
        response = client.get(f"{settings.gorse_base_url}/", headers=headers)
    return {
        "ok": response.status_code < 500,
        "configured": True,
        "base_url": settings.gorse_base_url,
        "status_code": response.status_code,
        "api_key_configured": bool(settings.gorse_server_api_key),
    }


def _check_freshrss() -> dict[str, Any]:
    settings = get_settings()
    if not settings.freshrss_configured:
        return {"ok": False, "configured": False, "base_url": settings.freshrss_base_url}
    client = FreshRSSClient(settings)
    with httpx.Client(timeout=8.0, follow_redirects=True) as http_client:
        client.authenticate(http_client)
    return {"ok": True, "configured": True, "base_url": settings.freshrss_base_url}


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
        "gorse": _timed_check("gorse", _check_gorse),
        "freshrss": _timed_check("freshrss", _check_freshrss),
        "invidious_api": _timed_check("invidious_api", _check_invidious_api),
        "invidious_player": _timed_check("invidious_player", _check_invidious_player),
        "recommended_feed": _timed_check("recommended_feed", _feed_status),
    }
    critical = ["database", "redis", "gorse", "freshrss", "invidious_api", "invidious_player", "recommended_feed"]
    counts = _json_ready_value(db.system_counts())
    return {
        "ok": all(bool(checks[name].get("ok")) for name in critical),
        "service": "OpenFeed",
        "version": __version__,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "checks": checks,
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
                    timeout=httpx.Timeout(None, connect=8.0),
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
    upstream_response = client.send(upstream_request, stream=True)
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


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    settings = get_settings()
    database = db.db_status()
    cache = _redis_status()
    return {
        "ok": database.get("ok") and cache.get("ok"),
        "service": "OpenFeed",
        "version": __version__,
        "database": database,
        "redis": cache,
        "freshrss_configured": settings.freshrss_configured,
    }


@app.get("/api/status/full")
def api_status_full() -> dict[str, Any]:
    return full_status_payload()


@app.get("/admin/status", response_class=HTMLResponse)
def admin_status() -> str:
    status = full_status_payload()
    checks = status["checks"]
    counts = status.get("counts") or {}

    def check_card(key: str, label: str) -> str:
        item = checks.get(key, {})
        state = "ok" if item.get("ok") else "bad"
        detail_parts = []
        for field in ("latency_ms", "base_url", "status_code", "configured", "api_key_configured", "items", "embed", "companion_manifest", "likely_cause", "playback_fallback", "fallback_serving", "error"):
            if item.get(field) is not None:
                detail_parts.append(f"{escape(str(field))}: {escape(str(item.get(field)))}")
        detail = "<br>".join(detail_parts)
        return f"""
        <article class="check {state}">
          <div><h2>{escape(label)}</h2><p>{detail}</p></div>
          <strong>{'OK' if item.get('ok') else 'FAIL'}</strong>
        </article>
        """

    count_rows = "".join(
        f"<tr><th>{escape(str(key).replace('_', ' '))}</th><td>{escape(str(value))}</td></tr>"
        for key, value in counts.items()
    )
    sync_rows = "".join(
        f"<tr><td>{run['id']}</td><td>{escape(run['status'])}</td><td>{escape(str(run.get('started_at') or ''))}</td><td>{escape(str(run.get('finished_at') or ''))}</td><td>{run['subscriptions_count']}</td><td>{run['entries_count']}</td><td>{escape(run.get('message') or '')}</td></tr>"
        for run in status.get("latest_sync_runs") or []
    ) or "<tr><td colspan='7'>No sync runs.</td></tr>"
    retrieval_rows = "".join(
        f"<tr><td>{escape(item.get('source') or '')}</td><td>{escape(item.get('status') or '')}</td><td>{escape(str(item.get('attempted_at') or ''))}</td><td>{escape(item.get('youtube_video_id') or '')}</td><td>{escape(item.get('message') or '')}</td></tr>"
        for item in status.get("recent_retrieval_attempts") or []
    ) or "<tr><td colspan='5'>No retrieval attempts.</td></tr>"
    generated_at = escape(str(status.get("generated_at") or ""))
    overall = "ok" if status.get("ok") else "bad"
    cards = "".join(
        [
            check_card("database", "Postgres"),
            check_card("redis", "Redis"),
            check_card("gorse", "Gorse"),
            check_card("freshrss", "FreshRSS"),
            check_card("invidious_api", "Invidious API"),
            check_card("invidious_player", "Invidious Player"),
            check_card("recommended_feed", "Recommended Feed"),
        ]
    )
    return f"""
    <!doctype html>
    <html lang="en">
      <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Status - OpenFeed</title>
        <style>
          :root {{ color-scheme: dark; font-family: Inter, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
          body {{ margin: 0; background: #101820; color: #eef2f7; }}
          header {{ padding: 18px 28px; border-bottom: 1px solid #314052; background: #0c1218; display: flex; justify-content: space-between; gap: 16px; align-items: center; }}
          main {{ max-width: 1180px; margin: 0 auto; padding: 24px; }}
          a {{ color: #8bd3ff; text-decoration: none; }}
          h1 {{ margin: 0; font-size: 24px; letter-spacing: 0; }}
          .overall {{ border: 1px solid #314052; border-radius: 8px; padding: 8px 11px; background: #17222e; }}
          .overall.ok {{ border-color: #2f8f5b; color: #a7f3c6; }}
          .overall.bad {{ border-color: #be123c; color: #fecdd3; }}
          .checks {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 12px; }}
          .check {{ border: 1px solid #314052; border-radius: 8px; background: #17222e; padding: 13px; display: flex; justify-content: space-between; gap: 10px; min-height: 104px; }}
          .check.ok {{ border-color: #2f8f5b; }}
          .check.bad {{ border-color: #be123c; }}
          .check h2 {{ margin: 0 0 8px; font-size: 15px; letter-spacing: 0; }}
          .check p {{ margin: 0; color: #b7c6d8; font-size: 12px; line-height: 1.5; overflow-wrap: anywhere; }}
          .check strong {{ font-size: 13px; }}
          section {{ margin-top: 24px; }}
          table {{ width: 100%; border-collapse: collapse; font-size: 13px; border: 1px solid #314052; background: #17222e; }}
          th, td {{ border-bottom: 1px solid #314052; padding: 8px; text-align: left; vertical-align: top; }}
          th {{ color: #b7c6d8; font-weight: 600; }}
          .two {{ display: grid; grid-template-columns: minmax(260px, 390px) 1fr; gap: 18px; align-items: start; }}
          @media (max-width: 820px) {{ header {{ align-items: flex-start; flex-direction: column; }} main {{ padding: 18px; }} .two {{ grid-template-columns: 1fr; }} }}
        </style>
      </head>
      <body>
        <header>
          <div><a href="/">OpenFeed</a><h1>System Status</h1><p>Generated {generated_at}</p></div>
          <div class="overall {overall}">{'All critical checks passing' if status.get('ok') else 'One or more critical checks failed'}</div>
        </header>
        <main>
          <section class="checks">{cards}</section>
          <section class="two">
            <div>
              <h2>Counts</h2>
              <table><tbody>{count_rows}</tbody></table>
            </div>
            <div>
              <h2>Latest Sync Runs</h2>
              <table><thead><tr><th>ID</th><th>Status</th><th>Started</th><th>Finished</th><th>Subs</th><th>Entries</th><th>Message</th></tr></thead><tbody>{sync_rows}</tbody></table>
            </div>
          </section>
          <section>
            <h2>Recent Retrieval Attempts</h2>
            <table><thead><tr><th>Source</th><th>Status</th><th>Attempted</th><th>Video</th><th>Message</th></tr></thead><tbody>{retrieval_rows}</tbody></table>
          </section>
        </main>
      </body>
    </html>
    """


# Icon geometry from Lucide v1.26.0 (ISC) — https://lucide.dev
# Vendored as path data so the app stays a single self-contained server render
# with no bundler, CDN request, or runtime dependency.
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
    "user": '<path d="M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2"/> <circle cx="12" cy="7" r="4"/>',
    "clock": '<circle cx="12" cy="12" r="10"/> <path d="M12 6v6l4 2"/>',
    "like": '<path d="M15 5.88 14 10h5.83a2 2 0 0 1 1.92 2.56l-2.33 8A2 2 0 0 1 17.5 22H4a2 2 0 0 1-2-2v-8a2 2 0 0 1 2-2h2.76a2 2 0 0 0 1.79-1.11L12 2a3.13 3.13 0 0 1 3 3.88Z"/> <path d="M7 10v12"/>',
    "dislike": '<path d="M9 18.12 10 14H4.17a2 2 0 0 1-1.92-2.56l2.33-8A2 2 0 0 1 6.5 2H20a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-2.76a2 2 0 0 0-1.79 1.11L12 22a3.13 3.13 0 0 1-3-3.88Z"/> <path d="M17 14V2"/>',
    "hide": '<path d="M10.733 5.076a10.744 10.744 0 0 1 11.205 6.575 1 1 0 0 1 0 .696 10.747 10.747 0 0 1-1.444 2.49"/> <path d="M14.084 14.158a3 3 0 0 1-4.242-4.242"/> <path d="M17.479 17.499a10.75 10.75 0 0 1-15.417-5.151 1 1 0 0 1 0-.696 10.75 10.75 0 0 1 4.446-5.143"/> <path d="m2 2 20 20"/>',
    "check": '<path d="M20 6 9 17l-5-5"/>',
    "external": '<path d="M15 3h6v6"/> <path d="M10 14 21 3"/> <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>',
    "share": '<circle cx="18" cy="5" r="3"/> <circle cx="6" cy="12" r="3"/> <circle cx="18" cy="19" r="3"/> <line x1="8.59" x2="15.42" y1="13.51" y2="17.49"/> <line x1="15.41" x2="8.59" y1="6.51" y2="10.49"/>',
    "download": '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/> <path d="M7 10l5 5 5-5"/> <path d="M12 15V3"/>',
    "theater": '<rect x="2" y="5" width="20" height="14" rx="2"/> <path d="M2 9h20"/>',
    "shorts": '<path d="M4 14a1 1 0 0 1-.78-1.63l9.9-10.2a.5.5 0 0 1 .86.46l-1.92 6.02A1 1 0 0 0 13 10h7a1 1 0 0 1 .78 1.63l-9.9 10.2a.5.5 0 0 1-.86-.46l1.92-6.02A1 1 0 0 0 11 14z"/>',
    "playlist": '<path d="M21 5H3"/> <path d="M10 12H3"/> <path d="M10 19H3"/> <path d="M15 12.003a1 1 0 0 1 1.517-.859l4.997 2.997a1 1 0 0 1 0 1.718l-4.997 2.997a1 1 0 0 1-1.517-.86z"/>',
    "later": '<circle cx="12" cy="12" r="10"/> <path d="M12 6v6l4 2"/>',
    "settings": '<path d="M9.671 4.136a2.34 2.34 0 0 1 4.659 0 2.34 2.34 0 0 0 3.319 1.915 2.34 2.34 0 0 1 2.33 4.033 2.34 2.34 0 0 0 0 3.831 2.34 2.34 0 0 1-2.33 4.033 2.34 2.34 0 0 0-3.319 1.915 2.34 2.34 0 0 1-4.659 0 2.34 2.34 0 0 0-3.32-1.915 2.34 2.34 0 0 1-2.33-4.033 2.34 2.34 0 0 0 0-3.831A2.34 2.34 0 0 1 6.35 6.051a2.34 2.34 0 0 0 3.319-1.915"/> <circle cx="12" cy="12" r="3"/>',
    "caption": '<rect width="18" height="14" x="3" y="5" rx="2" ry="2"/> <path d="M7 15h4M15 15h2M7 11h2M13 11h4"/>',
    "spark": '<path d="M11.017 2.814a1 1 0 0 1 1.966 0l1.051 5.558a2 2 0 0 0 1.594 1.594l5.558 1.051a1 1 0 0 1 0 1.966l-5.558 1.051a2 2 0 0 0-1.594 1.594l-1.051 5.558a1 1 0 0 1-1.966 0l-1.051-5.558a2 2 0 0 0-1.594-1.594l-5.558-1.051a1 1 0 0 1 0-1.966l5.558-1.051a2 2 0 0 0 1.594-1.594z"/> <path d="M20 2v4"/> <path d="M22 4h-4"/> <circle cx="4" cy="20" r="2"/>',
    "x": '<path d="M18 6 6 18"/> <path d="m6 6 12 12"/>',
    "subs": '<path d="M15.033 9.44a.647.647 0 0 1 0 1.12l-4.065 2.352a.645.645 0 0 1-.968-.56V7.648a.645.645 0 0 1 .967-.56z"/> <path d="M7 21h10"/> <rect width="20" height="14" x="2" y="3" rx="2"/>',
    "history": '<path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/> <path d="M3 3v5h5"/> <path d="M12 7v5l4 2"/>',
    "collections": '<rect width="7" height="7" x="3" y="3" rx="1"/> <rect width="7" height="7" x="14" y="3" rx="1"/> <rect width="7" height="7" x="14" y="14" rx="1"/> <rect width="7" height="7" x="3" y="14" rx="1"/>',
    "grid-view": '<rect width="7" height="7" x="3" y="3" rx="1"/> <rect width="7" height="7" x="14" y="3" rx="1"/> <rect width="7" height="7" x="14" y="14" rx="1"/> <rect width="7" height="7" x="3" y="14" rx="1"/>',
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


def _sidebar_channels(limit: int = 7) -> list[dict[str, Any]]:
    try:
        rows = db.list_subscriptions(limit * 3)
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


def _sidebar_subscription_group() -> str:
    """Rendered on its own so /api/sidebar/subscriptions can swap it in after a
    subscription change without reloading the page."""
    channels = _sidebar_channels()
    if not channels:
        return ""
    rows = []
    for channel in channels:
        name = str(channel.get("title") or "Channel")
        channel_id = escape(str(channel.get("youtube_channel_id") or ""))
        dot = '<span class="nav-dot" aria-hidden="true"></span>' if _is_recent(channel.get("latest_video_at")) else ""
        avatar = _avatar_html(name, channel.get("channel_avatar_url") or channel.get("avatar_url"))
        rows.append(
            f'<a class="nav-item nav-channel" href="/channel/{channel_id}">'
            f'{avatar}<span>{escape(name)}</span>{dot}</a>'
        )
    return (
        '<nav class="nav-group" data-subscription-nav>'
        f'<p class="nav-group-title">Subscriptions</p>{"".join(rows)}</nav>'
    )


def _home_nav(active: str = "home") -> str:
    groups = [
        [
            ("home", "Home", "/", "home"),
            ("explore", "Explore", "/recommendations", "discover"),
            ("shorts", "Shorts", "/shorts", "shorts"),
            ("channels", "Subscriptions", "/channels", "subs"),
            ("library", "Library", "/library", "library"),
        ],
        [
            ("history", "History", "/library#history", "history"),
            ("liked", "Liked videos", "/library#liked", "like"),
            ("playlists", "Playlists", "/library#playlists", "playlist"),
        ],
        [
            ("collections", "Collections", "/channels#collections", "collections"),
            ("status", "Status", "/admin/status", "status"),
        ],
    ]
    blocks: list[str] = []
    for group in groups:
        items = []
        for key, label, href, icon in group:
            current = ' aria-current="page"' if key == active else ""
            item_class = " active" if key == active else ""
            items.append(f'<a class="nav-item{item_class}" href="{href}"{current}>{_icon(icon)}<span>{escape(label)}</span></a>')
        blocks.append(f'<nav class="nav-group">{"".join(items)}</nav>')

    blocks.append(_sidebar_subscription_group())
    return "".join(blocks)


def _mobile_nav() -> str:
    links = [
        ("Home", "/", "home"),
        ("Subs", "/channels", "subs"),
        ("Library", "/library", "library"),
        ("Status", "/admin/status", "status"),
    ]
    return "".join(f'<a class="mobile-nav-item" href="{href}">{_icon(icon)}<span>{escape(label)}</span></a>' for label, href, icon in links)


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
    trailing_html = f'<span class="menu-trailing">{escape(trailing)}</span>' if trailing else "<span></span>"
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


def _watch_options_menu(video_id_raw: str, invidious_url: str, youtube_url: str) -> str:
    video_id = escape(video_id_raw)
    rows = [
        _menu_row("external", "Open in Invidious", href=invidious_url),
        _menu_row("external", "Open original", href=youtube_url),
        _menu_row("share", "Copy OpenFeed link", attrs=f' data-copy-url="/watch/{video_id}"'),
        _menu_row("share", "Copy original link", attrs=f' data-copy-url="{youtube_url}"'),
        '<div class="menu-separator" role="separator"></div>',
        _menu_row("check", "Mark watched", attrs=f' data-feedback="mark_watched" data-video-id="{video_id}"'),
        _menu_row("block", "Not interested", attrs=f' data-feedback="hide" data-video-id="{video_id}"', danger=True),
    ]
    return f'''
      <details class="watch-menu yt-menu">
        <summary class="text-action menu-summary" aria-label="Open watch options">{_icon("settings")}<span>Options</span></summary>
        <div class="menu-panel" role="menu" aria-label="Watch options">{"".join(rows)}</div>
      </details>
    '''


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
            {_menu_row("library", "Library", href="/library")}
            {_menu_row("subs", "Subscriptions", href="/channels")}
            {_menu_row("status", "Admin status", href="/admin/status")}
            <div class="menu-separator" role="separator"></div>
            {_menu_row("check", "User key", trailing=auth_sub[:22], attrs=" disabled").replace('class="menu-row"', 'class="menu-row disabled"')}
          </div>
        </details>
      </div>
    '''


def _feed_dialog(user_key: str = "local") -> str:
    preferences = db.get_feed_preferences(user_key)
    preferred = ", ".join(preferences.get("preferred_topics") or [])
    muted = ", ".join(preferences.get("muted_topics") or [])
    prioritize_checked = " checked" if preferences.get("prioritize_subscriptions", True) else ""
    shorts_checked = " checked" if preferences.get("exclude_shorts", False) else ""
    discovery_level = max(0, min(100, int(float(preferences.get("discovery_level", 0.35)) * 100)))
    return f"""
      <dialog class="feed-dialog tune-dialog" id="feed-dialog" aria-labelledby="feed-dialog-title">
        <form id="feed-preferences-form" method="dialog" data-tune-root style="--tune: {discovery_level}; --tune-hue: {190 + discovery_level * 1.65:.0f};">
          <div class="tune-hero">
            <span class="tune-aurora" aria-hidden="true"></span>
            <div class="dialog-head">
              <div>
                <p class="tune-eyebrow">Feed controls</p>
                <h2 id="feed-dialog-title">Personalize your feed</h2>
                <p class="tune-sub">Set how far OpenFeed wanders from the channels you follow. Everything here applies the moment you save.</p>
              </div>
              <button class="icon-button" type="button" data-close-dialog aria-label="Close">{_icon("x")}</button>
            </div>
            <section class="tune-card" aria-label="Discovery balance">
              <header class="tune-card-head">
                <div>
                  <p class="tune-card-label">Discovery</p>
                  <p class="tune-state" data-tune-state aria-live="polite">Balanced</p>
                </div>
                <span class="tune-readout" data-tune-readout>{discovery_level}%</span>
              </header>
              <div class="tune-slider">
                <span class="tune-track" aria-hidden="true"><span class="tune-fill" data-tune-fill></span></span>
                <input class="tune-range" name="discovery_level" type="range" min="0" max="100" value="{discovery_level}" data-tune-range aria-label="Discovery level">
              </div>
              <div class="tune-scale" aria-hidden="true"><span>Familiar</span><span>Balanced</span><span>Wide open</span></div>
              <div class="tune-mix" aria-hidden="true">
                <span class="tune-mix-bar"><span class="tune-mix-known" data-tune-known></span><span class="tune-mix-new" data-tune-new></span></span>
                <div class="tune-mix-legend">
                  <span><i class="tune-dot known"></i>Subscriptions &amp; close matches <b data-tune-known-pct>65%</b></span>
                  <span><i class="tune-dot new"></i>New to you <b data-tune-new-pct>35%</b></span>
                </div>
              </div>
            </section>
          </div>
          <div class="dialog-body dialog-grid">
            <section class="dialog-section dialog-card" aria-label="Feed sources">
              <h3>Sources</h3>
              <label class="toggle-row"><span><strong>Prioritize subscriptions</strong><small>Anchor the feed to subscribed channels and the topics they cover.</small></span><input name="prioritize_subscriptions" type="checkbox" data-tune-input{prioritize_checked}></label>
              <label class="toggle-row"><span><strong>Exclude Shorts</strong><small>Hide videos under 90 seconds from every recommendation section.</small></span><input name="exclude_shorts" type="checkbox" data-tune-input{shorts_checked}></label>
            </section>
            <section class="dialog-section dialog-card" aria-label="Topic preferences">
              <h3>Topics</h3>
              <label class="text-field">Follow<input name="preferred_topics" type="text" value="{escape(preferred, quote=True)}" placeholder="technology, cooking, music" data-topic-input="follow"></label>
              <div class="topic-chip-list" data-topic-chips="follow"></div>
              <label class="text-field">Mute<input name="muted_topics" type="text" value="{escape(muted, quote=True)}" placeholder="drama, prank, reaction" data-topic-input="mute"></label>
              <div class="topic-chip-list muted" data-topic-chips="mute"></div>
              <p class="dialog-note">Comma separated. Followed topics get a visible boost; muted topics take a heavy ranking penalty.</p>
            </section>
            <section class="dialog-section dialog-card" aria-label="Signals collected">
              <h3>Signals in play</h3>
              <div class="signal-list"><span>Subscriptions</span><span>Watch progress</span><span>Likes &amp; dislikes</span><span>Not interested</span><span>Gorse personalization</span></div>
            </section>
          </div>
          <div class="dialog-actions"><p id="feed-preferences-status" class="status-line"></p><button type="button" data-reset-feed-preferences>Reset</button><button class="primary-action tune-save" type="submit">Save changes</button></div>
        </form>
      </dialog>
    """


def _settings_dialog(user: dict[str, Any] | None = None, user_key: str = "local") -> str:
    settings = get_settings()
    user = user or _current_user(None)
    try:
        _preferences = db.get_feed_preferences(user_key)
        comments_checked = " checked" if _preferences.get("show_comments") else ""
        # Stored as exclude_shorts; surfaced as the friendlier positive "Show Shorts".
        shorts_checked = "" if _preferences.get("exclude_shorts") else " checked"
    except Exception:
        comments_checked = ""
        shorts_checked = " checked"
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
    freshrss = "Connected" if settings.freshrss_configured else "Not configured"
    panes = [
        ("playback", "Playback", "play"),
        ("content", "Content", "home"),
        ("account", "Account", "user"),
        ("services", "Services", "status"),
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
            </section>

            <section class="settings-pane" data-settings-pane-body="content" role="tabpanel">
              <header class="settings-pane-head">
                <h3>Content</h3>
                <p>What shows up in your feed and on watch pages.</p>
              </header>
              <label class="toggle-row"><span><strong>Show Shorts</strong><small>Include vertical Shorts, with their own shelf on Home. Off hides them everywhere.</small></span><input type="checkbox" data-setting-shorts{shorts_checked}></label>
              <label class="toggle-row"><span><strong>Show comments</strong><small>Load comments on watch pages through Invidious, only when you open a video.</small></span><input type="checkbox" data-setting-comments{comments_checked}></label>
              <p class="dialog-note" data-comments-status></p>
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
              <dl class="settings-list"><dt>FreshRSS</dt><dd>{freshrss}</dd><dt>Invidious</dt><dd>{escape(settings.invidious_public_url or "Not configured")}</dd><dt>Gorse</dt><dd>{escape(settings.gorse_base_url or "Not configured")}</dd></dl>
              <div class="settings-pane-actions"><a class="text-action" href="/admin/status">{_icon("status")}<span>Full status page</span></a></div>
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


def _home_video_card(video: dict[str, Any], position: int, settings: Any, variant: str = "grid") -> str:
    title_text = str(video.get("title") or "Untitled")
    title = escape(title_text)
    channel_text = str(video.get("channel_title") or video.get("author") or "Unknown channel")
    channel = escape(channel_text)
    video_id_raw = str(video.get("youtube_video_id") or "")
    video_id = escape(video_id_raw)
    # Shorts open in the immersive Shorts feed; everything else opens the watch page.
    watch_href = (f"/shorts?start={video_id}" if db.is_short_video(video) else f"/watch/{video_id}") if video_id else "#"
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


def _dedupe_section_videos(videos: list[dict[str, Any]], seen: set[str], limit: int) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for video in videos:
        video_id = str(video.get("youtube_video_id") or "")
        if not video_id or video_id in seen:
            continue
        seen.add(video_id)
        selected.append(video)
        if len(selected) >= limit:
            break
    return selected


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

    Prefers entries FreshRSS still marks unread. FreshRSS installs that mark
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
        videos = _dedupe_section_videos(_catch_up_videos(shelf["key"], pool, user_key), seen, 8)
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

        # The top of the feed is always the recommendation run, with subscription
        # uploads woven through it rather than parked in their own shelf below.
        recommended = _blend_subscription_videos(
            _without_shorts(db.list_recommended_videos(HOME_PAGE_SIZE + 12, 0, "all", user_key)),
            subscription_pool,
            HOME_PAGE_SIZE,
            cadence,
        )
        top_videos = _dedupe_section_videos(recommended, seen, HOME_PAGE_SIZE)
        if top_videos:
            sections.append({"id": "recommended", "eyebrow": "For you", "title": "Recommended for you", "videos": top_videos, "layout": "grid", "kind": "recommended"})

        # Reserved before the remaining shelves so a catch-up variant is never
        # starved of videos, then placed further down the page below.
        catch_up = _catch_up_section(subscription_pool, seen, user_key, seed, page)

        continue_videos = _dedupe_section_videos(_without_shorts(db.list_recommended_videos(8, 0, "continue", user_key)), seen, 8)
        if continue_videos:
            sections.append({"id": "continue-watching", "eyebrow": "Resume", "title": "Continue watching", "videos": continue_videos, "layout": "grid", "kind": "continue"})
        sub_videos = _dedupe_section_videos(subscription_pool, seen, 12)
        if sub_videos:
            sections.append({"id": "new-subscriptions", "eyebrow": "Subscriptions", "title": "New from your subscriptions", "videos": sub_videos, "layout": "grid", "kind": "subscription"})
        if shorts:
            sections.append({"id": "shorts", "eyebrow": "Shorts", "title": "Shorts", "videos": shorts, "layout": "shorts", "kind": "shorts"})
        more = _dedupe_section_videos(
            _blend_subscription_videos(
                _without_shorts(db.list_recommended_videos(HOME_PAGE_SIZE, HOME_PAGE_SIZE, "all", user_key)),
                subscription_pool,
                HOME_PAGE_SIZE,
                cadence,
            ),
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
        lane, title, eyebrow = "all", "More from channels you like", "Personalized"
        section_id = f"channels-you-like-{page}"
    elif page % 4 == 2:
        lane, title, eyebrow = "recent", "Recently uploaded in your interests", "Fresh"
        section_id = f"recent-interests-{page}"
    elif page % 4 == 3:
        lane, title, eyebrow = "long-form", "Long-form picks", "Deep watch"
        section_id = f"long-form-{page}"
    else:
        lane, title, eyebrow = "all", "Personalized discovery", "Explore"
        section_id = f"discovery-{page}"

    videos = db.list_recommended_videos(HOME_PAGE_SIZE, offset, lane, user_key)
    if len(videos) < 8 and lane != "all":
        # A narrow lane runs dry long before the library does. Top it up rather than
        # returning an empty page, which would stop infinite scroll for good.
        videos = videos + db.list_recommended_videos(HOME_PAGE_SIZE, offset, "all", user_key)
        title, eyebrow = "More recommendations", "Keep going"
        section_id = f"more-{page}"

    # Deeper pages keep the same blend so subscriptions never fall out of the feed.
    # Start past what page 0 already showed, or every page would inject the same
    # uploads and the client-side dedupe would just drop them again.
    subscription_slice = subscription_pool[min(len(subscription_pool), 42 + (page - 1) * 8):]
    videos = _dedupe_section_videos(
        _blend_subscription_videos(_without_shorts(videos), subscription_slice, HOME_PAGE_SIZE, cadence),
        seen,
        HOME_PAGE_SIZE,
    )
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
      h1, h2, h3 { letter-spacing: -.018em; font-weight: 600; }
      .icon { width: 20px; height: 20px; fill: none; stroke: currentColor; stroke-width: 1.75; stroke-linecap: round; stroke-linejoin: round; flex: 0 0 auto; }
      :focus-visible { outline: none; box-shadow: var(--focus); }
      ::-webkit-scrollbar { width: 10px; height: 10px; }
      ::-webkit-scrollbar-thumb { background: #2a2a2e; border: 3px solid var(--bg); border-radius: 999px; }
      ::-webkit-scrollbar-thumb:hover { background: #35353a; }

      /* ---------- top bar ---------- */
      .app-header { position: fixed; top: 0; left: 0; right: 0; z-index: 60; height: var(--header); background: rgba(10,10,11,.86); backdrop-filter: saturate(160%) blur(14px); border-bottom: 1px solid var(--border-soft); display: grid; grid-template-columns: minmax(150px, 1fr) minmax(0, 560px) minmax(150px, 1fr); align-items: center; gap: 16px; padding: 0 16px; }
      .header-left { display: flex; align-items: center; gap: 6px; min-width: 0; }
      .brand { display: inline-flex; align-items: center; gap: 8px; padding: 0 6px; font-size: 15px; font-weight: 650; letter-spacing: -.02em; color: var(--text); }
      .brand-mark { display: grid; place-items: center; width: 22px; height: 22px; border-radius: 6px; background: var(--accent); color: #fff; font-size: 9px; font-weight: 700; letter-spacing: .02em; }
      .header-center { display: flex; align-items: center; gap: 8px; justify-content: center; min-width: 0; }
      .search-form { flex: 1; min-width: 0; height: 36px; display: flex; align-items: center; border: 1px solid var(--border); border-radius: 999px; background: var(--surface); overflow: hidden; transition: border-color .12s ease, background .12s ease; }
      .search-form:hover { background: var(--surface-2); }
      .search-form:focus-within { border-color: rgba(255,255,255,.22); background: var(--surface-2); }
      .search-form input { flex: 1; min-width: 0; height: 100%; border: 0; background: transparent; color: var(--text); padding: 0 16px; outline: none; font-size: 13.5px; }
      .search-form input::placeholder { color: var(--muted-2); }
      .search-form button { width: 52px; height: 100%; border: 0; border-left: 1px solid var(--border-soft); background: var(--surface-2); display: grid; place-items: center; cursor: pointer; color: var(--muted); }
      .search-form button:hover { color: var(--text); background: var(--surface-3); }
      .header-mic { display: none; }
      .header-actions { display: flex; justify-content: flex-end; align-items: center; gap: 2px; }
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
      .nav-item { position: relative; min-height: 38px; border-radius: 10px; display: flex; align-items: center; gap: 16px; padding: 0 12px; color: var(--text); font-size: 13.5px; transition: background .12s ease, color .12s ease; }
      .nav-item:hover { background: var(--surface-2); }
      .nav-item.active { background: var(--surface-3); font-weight: 500; }
      .nav-item.active .icon { color: var(--accent); }
      .nav-item .icon { width: 20px; height: 20px; color: var(--muted); }
      .nav-channel { gap: 12px; min-height: 38px; }
      .nav-channel span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .nav-channel .avatar { width: 24px; height: 24px; font-size: 9.5px; }
      .nav-dot { width: 5px; height: 5px; margin-left: auto; border-radius: 999px; background: var(--accent); flex: 0 0 auto; }
      .sidebar-meta { color: var(--muted-2); font-size: 11.5px; line-height: 1.6; padding: 14px 10px 0; }
      .sidebar-meta p { margin: 0 0 4px; }
      .sidebar-meta .source-offer { margin-top: 10px; }
      .sidebar-meta .source-offer a { color: var(--muted-2); text-decoration: none; border-bottom: 1px solid transparent; }
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
      .topic-chip { height: 32px; border: 1px solid transparent; border-radius: 8px; background: var(--surface-2); color: var(--text); padding: 0 12px; white-space: nowrap; cursor: pointer; display: inline-flex; align-items: center; justify-content: center; gap: 6px; font-size: 13.5px; line-height: 1; transition: background .12s ease, color .12s ease; }
      .topic-chip:hover { background: var(--surface-3); }
      .topic-chip.active { background: #f1f1f3; color: #0a0a0b; font-weight: 500; }
      .topic-action { flex: 0 0 auto; width: 32px; padding: 0; color: var(--text); }
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
      .video-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(288px, 1fr)); gap: 34px 20px; align-items: start; }
      .video-card { min-width: 0; color: var(--text); }
      .thumb { position: relative; display: grid; place-items: center; aspect-ratio: 16 / 9; overflow: hidden; border-radius: var(--r-thumb); background: #000; border: 1px solid var(--border-soft); color: #fff; }
      .thumb img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; }
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
      .metadata { margin-top: 2px; color: var(--muted-2); font-variant-numeric: tabular-nums; }
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
      .card-menu { justify-self: end; }
      .menu-trigger { width: 22px; height: 26px; border-radius: 6px; display: grid; place-items: center; color: var(--muted); }
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
      .feed-loading, .feed-retry { min-height: 48px; margin: 32px auto 0; color: var(--muted-2); display: flex; align-items: center; justify-content: center; gap: 10px; font-size: 12.5px; }
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
      .settings-dialog { width: min(860px, calc(100vw - 32px)); overflow: hidden; }
      .settings-dialog[open] { animation: tune-in .22s cubic-bezier(.2,.9,.3,1); }
      .settings-dialog::backdrop { background: rgba(0,0,0,.66); backdrop-filter: blur(6px); }
      .settings-shell { display: grid; grid-template-columns: 208px minmax(0, 1fr); min-height: 470px; max-height: min(84vh, 720px); }
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
      .toggle-row input[type=checkbox]:checked, .source-row input[type=checkbox]:checked { background: linear-gradient(120deg, hsl(var(--tune-hue, 352) 72% 46%), hsl(calc(var(--tune-hue, 352) + 26) 78% 54%)); border-color: transparent; box-shadow: 0 0 14px hsl(var(--tune-hue, 352) 80% 50% / .35); }
      .toggle-row input[type=checkbox]:checked::after, .source-row input[type=checkbox]:checked::after { transform: translateX(17px); background: #fff; }
      .toggle-row input[type=checkbox]:focus-visible, .source-row input[type=checkbox]:focus-visible { box-shadow: var(--focus); }
      .toggle-row:has(input[type=checkbox]:checked) { border-color: hsl(var(--tune-hue, 352) 60% 50% / .35); }
      .toggle-row { transition: border-color .2s ease, background .2s ease; }

      /* ---------- personalize feed dialog ---------- */
      .tune-dialog { width: min(760px, calc(100vw - 32px)); overflow: hidden; }
      .tune-dialog[open] { animation: tune-in .22s cubic-bezier(.2,.9,.3,1); }
      @keyframes tune-in { from { opacity: 0; transform: translateY(10px) scale(.985); } to { opacity: 1; transform: none; } }
      .tune-dialog::backdrop { background: rgba(0,0,0,.66); backdrop-filter: blur(6px); }
      .tune-hero { position: relative; overflow: hidden; padding-bottom: 18px; border-bottom: 1px solid var(--border-soft); background:
        radial-gradient(120% 140% at 8% -10%, hsl(var(--tune-hue) 70% 22% / .55), transparent 62%),
        radial-gradient(90% 120% at 100% 0%, hsl(calc(var(--tune-hue) + 40) 70% 24% / .35), transparent 60%),
        var(--surface); transition: background .35s ease; }
      .tune-aurora { position: absolute; inset: -40% -10% auto -10%; height: 220px; pointer-events: none; background: radial-gradient(50% 100% at 50% 50%, hsl(var(--tune-hue) 90% 55% / .22), transparent 70%); filter: blur(18px); opacity: .9; transform: translateX(calc((var(--tune) - 50) * .6%)); transition: transform .35s cubic-bezier(.2,.8,.3,1), background .35s ease; }
      .tune-hero .dialog-head { border-bottom: 0; padding-bottom: 4px; }
      .tune-eyebrow { margin: 0 0 6px; font-size: 11px; font-weight: 600; letter-spacing: .08em; text-transform: uppercase; color: hsl(var(--tune-hue) 80% 72%); transition: color .35s ease; }
      .tune-sub { margin: 8px 0 0; max-width: 46ch; color: var(--muted); font-size: 12.5px; line-height: 1.55; }
      .tune-card { position: relative; margin: 4px 20px 0; padding: 16px 18px 18px; border: 1px solid hsl(var(--tune-hue) 60% 60% / .18); border-radius: 14px; background: rgba(10,10,11,.55); backdrop-filter: blur(8px); display: grid; gap: 12px; transition: border-color .35s ease, box-shadow .35s ease; }
      .tune-card-head { display: flex; align-items: flex-end; justify-content: space-between; gap: 14px; }
      .tune-card-label { margin: 0; font-size: 11px; font-weight: 600; letter-spacing: .08em; text-transform: uppercase; color: var(--muted-2); }
      .tune-state { margin: 3px 0 0; font-size: 16px; font-weight: 600; letter-spacing: -.014em; color: var(--text); }
      .tune-readout { font-variant-numeric: tabular-nums; font-size: 22px; font-weight: 650; letter-spacing: -.02em; color: hsl(var(--tune-hue) 85% 66%); transition: color .3s ease, transform .18s ease; }
      .tune-slider { position: relative; height: 26px; display: flex; align-items: center; }
      .tune-track { position: absolute; left: 0; right: 0; height: 8px; border-radius: 999px; background: linear-gradient(90deg, rgba(255,255,255,.09), rgba(255,255,255,.05)); overflow: hidden; }
      /* The thumb travels inset by half its own width at each end, so the fill has to
         use the same geometry or the colour stops short of (or runs past) the handle. */
      .tune-fill { display: block; height: 100%; width: calc(10px + (100% - 20px) * var(--tune) / 100); border-radius: 999px; background: linear-gradient(90deg, hsl(calc(var(--tune-hue) - 32) 78% 48%), hsl(var(--tune-hue) 88% 58%)); box-shadow: 0 0 16px hsl(var(--tune-hue) 90% 55% / .45); transition: width .12s linear, background .3s ease, box-shadow .3s ease; }
      .tune-range { position: relative; z-index: 2; width: 100%; margin: 0; background: transparent; appearance: none; -webkit-appearance: none; cursor: grab; }
      .tune-range:active { cursor: grabbing; }
      .tune-range::-webkit-slider-runnable-track { height: 8px; background: transparent; }
      .tune-range::-moz-range-track { height: 8px; background: transparent; }
      .tune-range::-webkit-slider-thumb { appearance: none; -webkit-appearance: none; width: 20px; height: 20px; margin-top: -6px; border-radius: 999px; background: #fff; border: 3px solid hsl(var(--tune-hue) 88% 56%); box-shadow: 0 2px 10px rgba(0,0,0,.55), 0 0 0 0 hsl(var(--tune-hue) 90% 55% / .5); transition: transform .16s cubic-bezier(.2,.8,.3,1.4), border-color .3s ease, box-shadow .2s ease; }
      .tune-range::-moz-range-thumb { width: 20px; height: 20px; border-radius: 999px; background: #fff; border: 3px solid hsl(var(--tune-hue) 88% 56%); box-shadow: 0 2px 10px rgba(0,0,0,.55); transition: transform .16s ease, border-color .3s ease; }
      .tune-range:focus-visible { outline: none; box-shadow: none; }
      .tune-range:focus-visible::-webkit-slider-thumb { box-shadow: 0 0 0 4px hsl(var(--tune-hue) 90% 55% / .35); }
      [data-tune-root].is-sliding .tune-range::-webkit-slider-thumb { transform: scale(1.22); box-shadow: 0 2px 12px rgba(0,0,0,.6), 0 0 0 8px hsl(var(--tune-hue) 90% 55% / .18); }
      [data-tune-root].is-sliding .tune-range::-moz-range-thumb { transform: scale(1.22); }
      [data-tune-root].is-sliding .tune-card { border-color: hsl(var(--tune-hue) 70% 60% / .4); box-shadow: 0 0 0 1px hsl(var(--tune-hue) 70% 55% / .16), 0 18px 40px hsl(var(--tune-hue) 80% 20% / .5); }
      [data-tune-root].is-sliding .tune-readout { transform: scale(1.06); }
      /* Grid, not space-between: with flex the middle label centres between the two
         end labels rather than on the track's midpoint, which reads as crooked. */
      .tune-scale { display: grid; grid-template-columns: 1fr auto 1fr; align-items: center; color: var(--muted-2); font-size: 11px; letter-spacing: .01em; }
      .tune-scale span:first-child { justify-self: start; }
      .tune-scale span:nth-child(2) { justify-self: center; opacity: .7; }
      .tune-scale span:last-child { justify-self: end; }
      .tune-mix { display: grid; gap: 8px; padding-top: 12px; border-top: 1px solid var(--border-soft); }
      .tune-mix-bar { display: flex; height: 10px; border-radius: 999px; overflow: hidden; background: var(--surface-3); }
      .tune-mix-bar > span { display: block; transition: width .28s cubic-bezier(.2,.8,.3,1), background .3s ease; }
      .tune-mix-known { background: linear-gradient(90deg, hsl(calc(var(--tune-hue) - 45) 62% 42%), hsl(calc(var(--tune-hue) - 20) 70% 50%)); }
      .tune-mix-new { background: linear-gradient(90deg, hsl(var(--tune-hue) 85% 55%), hsl(calc(var(--tune-hue) + 30) 88% 62%)); }
      .tune-mix-legend { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 8px; color: var(--muted); font-size: 11.5px; }
      .tune-mix-legend span { display: inline-flex; align-items: center; gap: 7px; }
      .tune-mix-legend b { color: var(--text); font-variant-numeric: tabular-nums; font-weight: 600; }
      .tune-dot { width: 8px; height: 8px; border-radius: 999px; }
      .tune-dot.known { background: hsl(calc(var(--tune-hue) - 30) 68% 48%); }
      .tune-dot.new { background: hsl(calc(var(--tune-hue) + 15) 88% 58%); }
      .topic-chip-list { display: flex; flex-wrap: wrap; gap: 6px; min-height: 0; }
      .topic-chip-list:empty { display: none; }
      .topic-chip-list button { min-height: 26px; display: inline-flex; align-items: center; gap: 6px; border: 1px solid hsl(var(--tune-hue) 60% 55% / .3); border-radius: 999px; background: hsl(var(--tune-hue) 70% 50% / .12); color: var(--text); padding: 0 8px 0 10px; font-size: 11.5px; cursor: pointer; animation: chip-in .18s ease; transition: background .15s ease, border-color .15s ease; }
      .topic-chip-list button:hover { background: hsl(var(--tune-hue) 70% 50% / .22); }
      .topic-chip-list button em { font-style: normal; opacity: .55; font-size: 13px; line-height: 1; }
      .topic-chip-list.muted button { border-color: rgba(255,255,255,.12); background: rgba(255,255,255,.05); color: var(--muted); text-decoration: line-through; text-decoration-color: rgba(255,255,255,.28); }
      @keyframes chip-in { from { opacity: 0; transform: translateY(3px) scale(.96); } to { opacity: 1; transform: none; } }
      .tune-save { background: linear-gradient(120deg, hsl(var(--tune-hue) 80% 52%), hsl(calc(var(--tune-hue) + 34) 85% 60%)); color: #fff; box-shadow: 0 6px 18px hsl(var(--tune-hue) 80% 40% / .38); transition: filter .15s ease, box-shadow .2s ease, background .3s ease; }
      .tune-save:hover { background: linear-gradient(120deg, hsl(var(--tune-hue) 80% 56%), hsl(calc(var(--tune-hue) + 34) 88% 64%)); filter: brightness(1.05); }
      @media (prefers-reduced-motion: reduce) {
        .tune-dialog[open] { animation: none; }
        .tune-fill, .tune-mix-bar > span, .tune-aurora, .tune-readout, .tune-range::-webkit-slider-thumb { transition: none; }
      }
      @media (max-width: 720px) {
        .tune-card { margin: 4px 14px 0; padding: 14px; }
        .tune-readout { font-size: 19px; }
        .tune-mix-legend { flex-direction: column; }
      }

      .dialog-actions { display: flex; justify-content: flex-end; align-items: center; gap: 8px; padding: 0 20px 20px; }
      .dialog-actions button { height: 34px; border: 0; border-radius: 999px; background: var(--surface-2); color: var(--text); padding: 0 16px; cursor: pointer; font-size: 13px; font-weight: 500; }
      .dialog-actions button:hover { background: var(--surface-3); }
      .dialog-actions .primary-action { background: #f1f1f3; color: #0a0a0b; font-weight: 600; }
      .dialog-actions .primary-action:hover { background: #fff; }
      .dialog-actions .tune-save { background: linear-gradient(120deg, hsl(var(--tune-hue) 80% 52%), hsl(calc(var(--tune-hue) + 34) 85% 60%)); color: #fff; box-shadow: 0 6px 18px hsl(var(--tune-hue) 80% 40% / .38); }
      .dialog-actions .tune-save:hover { background: linear-gradient(120deg, hsl(var(--tune-hue) 80% 56%), hsl(calc(var(--tune-hue) + 34) 88% 64%)); filter: brightness(1.05); }
      .picker-list { display: grid; gap: 2px; max-height: 320px; overflow: auto; }
      .picker-row { display: grid; grid-template-columns: 32px minmax(0, 1fr) auto; align-items: center; gap: 12px; min-height: 46px; padding: 0 12px; border: 0; border-radius: 10px; background: transparent; color: var(--text); text-align: left; cursor: pointer; font-size: 13px; }
      .picker-row:hover { background: var(--surface-3); }
      .picker-row small { display: block; color: var(--muted-2); font-size: 11.5px; }
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
      .toolbar-note { color: var(--muted-2); font-size: 12.5px; display: inline-flex; align-items: center; gap: 7px; }
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
      .media-row p { margin: 6px 0 0; color: var(--muted-2); font-size: 12.5px; line-height: 1.4; }
      .row-label { display: none; }
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
      .channel-card { display: grid; justify-items: center; text-align: center; gap: 8px; padding: 14px 8px; border: 1px solid transparent; border-radius: var(--r-panel); min-width: 0; transition: background .12s ease, border-color .12s ease; }
      .channel-card:hover { background: var(--surface); border-color: var(--border-soft); }
      .channel-card-top { display: grid; justify-items: center; gap: 10px; min-width: 0; width: 100%; }
      .channel-card-top > div { min-width: 0; width: 100%; }
      .channel-card .avatar { width: 56px; height: 56px; font-size: 15px; }
      .channel-card h2 { margin: 0; font-size: 13px; font-weight: 550; line-height: 1.35; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .channel-card p { margin: 4px 0 0; color: var(--muted-2); font-size: 11.5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
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
      .stat-row { display: flex; align-items: center; gap: 18px; margin: 0 0 24px; color: var(--muted-2); font-size: 12.5px; }
      .stat { display: inline-flex; align-items: baseline; gap: 6px; }
      .stat strong { font-size: 13px; font-weight: 600; color: var(--text); font-variant-numeric: tabular-nums; }
      .stat span { color: var(--muted-2); }

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
      .collection-card-body p { margin: 5px 0 0; color: var(--muted-2); font-size: 12px; line-height: 1.4; }
      .collection-hero { display: flex; align-items: center; gap: 24px; padding: 4px 0 26px; border-bottom: 1px solid var(--border-soft); margin-bottom: 8px; }
      .collection-hero-cover { flex: 0 0 auto; width: 172px; aspect-ratio: 16 / 10; border: 1px solid var(--border-soft); border-radius: var(--r-panel); overflow: hidden; background: var(--surface); }
      .collection-hero-copy { min-width: 0; }
      .collection-hero-kicker { margin: 0 0 6px; color: var(--muted-2); font-size: 11px; font-weight: 600; letter-spacing: .08em; text-transform: uppercase; }
      .collection-hero h1 { margin: 0; font-size: clamp(20px, 2vw, 26px); font-weight: 650; letter-spacing: -.022em; }
      .collection-hero-meta { margin: 8px 0 0; color: var(--muted-2); font-size: 12.5px; }
      .collection-hero-description { margin: 10px 0 0; color: var(--muted); font-size: 13px; line-height: 1.55; max-width: 60ch; }
      .collection-hero-actions { margin-top: 16px; display: flex; gap: 8px; flex-wrap: wrap; }
      .collection-members .channel-card { position: relative; }
      .collection-remove { position: absolute; top: 6px; right: 6px; width: 24px; height: 24px; border: 0; border-radius: 999px; background: rgba(10,10,11,.8); color: var(--muted); display: grid; place-items: center; cursor: pointer; opacity: 0; transition: opacity .12s ease, color .12s ease; }
      .collection-members .channel-card:hover .collection-remove, .collection-remove:focus-visible { opacity: 1; }
      .collection-remove:hover { color: #f2686c; }
      .collection-remove .icon { width: 14px; height: 14px; }
      .picker-row.is-member .picker-check { color: var(--accent); }
      .picker-check { display: grid; place-items: center; width: 20px; }
      .picker-check .icon { width: 16px; height: 16px; }

      /* ---------- playlists ---------- */
      .playlist-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(248px, 1fr)); gap: 28px 20px; align-items: start; }
      .playlist-card { position: relative; min-width: 0; }
      .playlist-cover { position: relative; display: block; aspect-ratio: 16 / 9; border-radius: var(--r-thumb); overflow: hidden; background: var(--surface-2); border: 1px solid var(--border-soft); }
      .playlist-cover::before, .playlist-cover::after { content: ""; position: absolute; left: 8px; right: 8px; border-radius: var(--r-thumb) var(--r-thumb) 0 0; background: var(--surface-3); border: 1px solid var(--border-soft); border-bottom: 0; }
      .playlist-cover::before { top: -7px; height: 8px; opacity: .5; left: 14px; right: 14px; }
      .playlist-cover::after { top: -3px; height: 6px; opacity: .8; }
      .playlist-cover img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; }
      .playlist-cover-empty { position: absolute; inset: 0; display: grid; place-items: center; color: var(--muted-2); }
      .playlist-cover-empty .icon { width: 22px; height: 22px; }
      .playlist-count { position: absolute; right: 0; bottom: 0; top: 0; width: 42%; display: grid; place-items: center; align-content: center; gap: 6px; background: rgba(0,0,0,.74); backdrop-filter: blur(1px); color: #fff; font-size: 13px; font-weight: 500; font-variant-numeric: tabular-nums; }
      .playlist-count .icon { width: 18px; height: 18px; opacity: .9; }
      .playlist-body { display: grid; grid-template-columns: minmax(0, 1fr) 22px; gap: 8px; align-items: start; padding-top: 11px; }
      .playlist-body h3 { margin: 0; font-size: 13.5px; font-weight: 550; line-height: 1.4; letter-spacing: -.008em; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
      .playlist-body p { margin: 5px 0 0; color: var(--muted-2); font-size: 12px; line-height: 1.4; }
      .playlist-detail { display: grid; grid-template-columns: minmax(280px, 340px) minmax(0, 1fr); gap: 32px; align-items: start; }
      .playlist-panel { position: sticky; top: calc(var(--header) + 28px); border: 1px solid var(--border-soft); border-radius: var(--r-panel); background: var(--surface); padding: 18px; display: grid; gap: 14px; }
      .playlist-panel .playlist-cover { margin-top: 8px; }
      .playlist-panel h1 { margin: 0; font-size: 19px; font-weight: 650; letter-spacing: -.02em; line-height: 1.3; }
      .playlist-panel .playlist-meta { margin: 0; color: var(--muted-2); font-size: 12.5px; }
      .playlist-panel .playlist-description { margin: 0; color: var(--muted); font-size: 12.5px; line-height: 1.55; }
      .playlist-panel-actions { display: flex; gap: 8px; flex-wrap: wrap; }
      .playlist-items { display: grid; gap: 2px; }
      .playlist-items .media-row { grid-template-columns: 24px 168px minmax(0, 1fr) 22px 26px; gap: 14px; align-items: start; }
      .playlist-items .media-row h2 { font-size: 13.5px; }
      .playlist-remove { width: 26px; height: 26px; margin-top: 4px; border: 0; border-radius: 999px; background: transparent; color: var(--muted-2); display: grid; place-items: center; cursor: pointer; }
      .playlist-remove:hover { background: var(--surface-3); color: #f2686c; }

      /* ---------- channel page ---------- */
      /* YouTube banner geometry. Invidious serves the desktop crop (2560x423, ~6.05:1).
         YouTube renders 2560x423 on desktop, 1855x423 on tablet and 1546x423 on mobile —
         all the same centred strip, progressively cropped at the sides. Matching those
         aspect ratios per breakpoint and cropping from the centre keeps the channel's
         "safe zone" (name, logo, tagline) on screen at every width, same as YouTube. */
      .channel-banner { aspect-ratio: 2560 / 423; width: 100%; border: 1px solid var(--border-soft); border-radius: var(--r-panel); background: linear-gradient(135deg, hsl(var(--avatar-hue, 240) 10% 15%), var(--bg) 74%); display: flex; align-items: center; justify-content: center; color: rgba(255,255,255,.14); font-size: clamp(18px, 2.6vw, 34px); font-weight: 700; letter-spacing: .24em; text-transform: uppercase; overflow: hidden; }
      .channel-hero { display: flex; align-items: center; gap: 22px; padding: 22px 0 10px; }
      .channel-hero > div { min-width: 0; }
      .channel-hero-avatar { width: 88px; height: 88px; font-size: 26px; }
      .channel-hero h1 { margin: 0; font-size: clamp(20px, 2vw, 26px); font-weight: 650; letter-spacing: -.022em; display: flex; align-items: center; gap: 8px; }
      .channel-hero-meta { margin: 7px 0 0; color: var(--muted-2); font-size: 12.5px; }
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
      .short-copy p { margin: 4px 0 0; color: var(--muted-2); font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
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
      .bell-badge { position: absolute; top: 1px; right: 1px; min-width: 15px; height: 15px; padding: 0 4px; border-radius: 999px; background: var(--accent); color: #fff; font-size: 9.5px; font-weight: 700; line-height: 15px; text-align: center; pointer-events: none; }
      .bell-badge[hidden] { display: none; }
      .notification-row { display: grid; grid-template-columns: 34px minmax(0, 1fr) 62px; gap: 11px; align-items: start; padding: 9px 10px; border-radius: 8px; color: var(--text); }
      .notification-row:hover { background: var(--surface-3); }
      .notification-row.unread { background: rgba(229,72,77,.07); }
      .notification-row .avatar { width: 34px; height: 34px; }
      .notification-copy { min-width: 0; }
      .notification-copy strong { display: block; font-size: 12.5px; font-weight: 600; line-height: 1.35; }
      .notification-copy span { display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; color: var(--text); font-size: 12.5px; line-height: 1.4; margin-top: 2px; }
      .notification-copy small { display: block; color: var(--muted-2); font-size: 11px; margin-top: 4px; }
      .notification-thumb { position: relative; aspect-ratio: 16 / 9; border-radius: 6px; overflow: hidden; background: #000; }
      .notification-thumb img { width: 100%; height: 100%; object-fit: cover; }
      .notification-empty { color: var(--muted-2); font-size: 12.5px; padding: 16px 10px; text-align: center; }

      /* ---------- comments ---------- */
      .comments { margin-top: 24px; }
      .comments-head { display: flex; align-items: baseline; gap: 10px; margin-bottom: 18px; }
      .comments-head h2 { margin: 0; font-size: 16px; font-weight: 600; letter-spacing: -.012em; }
      .comments-count { color: var(--muted-2); font-size: 13px; font-variant-numeric: tabular-nums; }
      .comments-state { color: var(--muted-2); font-size: 13px; margin: 0; padding: 12px 0; }
      .comment { display: grid; grid-template-columns: 36px minmax(0, 1fr); gap: 14px; padding: 12px 0; }
      .comment .avatar { width: 36px; height: 36px; }
      .comment-head { display: flex; align-items: center; gap: 7px; flex-wrap: wrap; margin-bottom: 5px; }
      .comment-author { font-size: 12.5px; font-weight: 600; color: var(--text); }
      .comment-author.owner { background: var(--surface-3); border-radius: 999px; padding: 2px 9px; }
      .comment-time { color: var(--muted-2); font-size: 12px; }
      .comment-flag { color: var(--muted-2); font-size: 11px; display: inline-flex; align-items: center; gap: 4px; }
      .comment-flag .icon { width: 13px; height: 13px; }
      .comment-body { margin: 0; color: var(--text); font-size: 13.5px; line-height: 1.55; white-space: pre-wrap; overflow-wrap: anywhere; }
      .comment-meta { display: flex; align-items: center; gap: 14px; margin-top: 8px; color: var(--muted-2); font-size: 12px; }
      .comment-meta span { display: inline-flex; align-items: center; gap: 5px; }
      .comment-meta .icon { width: 14px; height: 14px; }

      /* ---------- watch ---------- */
      .watch-main { max-width: 1760px; padding-top: calc(var(--header) + 20px); padding-right: 32px; }
      .watch-layout { display: grid; grid-template-columns: minmax(0, 1fr) clamp(320px, 27vw, 392px); gap: 28px; align-items: start; min-width: 0; }
      .watch-layout > * { min-width: 0; }
      .watch-content { min-width: 0; }
      .watch-player-shell { position: relative; z-index: 1; background: #000; border: 1px solid var(--border-soft); border-radius: var(--r-panel); overflow: hidden; }
      .player-frame { width: 100%; max-width: 100%; aspect-ratio: 16 / 9; border: 0; background: #000; display: block; }
      .watch-title { margin: 16px 0 12px; font-size: 19px; font-weight: 650; line-height: 1.35; letter-spacing: -.018em; }
      .watch-meta-line { display: flex; align-items: center; justify-content: space-between; gap: 16px; flex-wrap: wrap; color: var(--muted); font-size: 13px; }
      .watch-channel { display: flex; align-items: center; gap: 11px; min-width: 0; }
      .watch-channel .avatar { width: 36px; height: 36px; }
      .watch-channel > div { min-width: 0; }
      .watch-channel-name { display: inline-block; max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text); font-size: 13.5px; font-weight: 550; }
      .watch-channel .metadata { font-size: 11.5px; margin-top: 2px; }
      .watch-inline-actions, .watch-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
      .watch-inline-actions { justify-content: flex-end; flex: 0 0 auto; }
      .watch-actions { margin: 16px 0; }
      .watch-actions button, .watch-actions a, .watch-actions summary, .watch-inline-actions button { min-height: 34px; border: 0; border-radius: 999px; background: var(--surface-2); color: var(--text); display: inline-flex; align-items: center; gap: 7px; padding: 0 14px; cursor: pointer; white-space: nowrap; font-size: 13px; font-weight: 500; transition: background .12s ease; }
      .watch-actions button:hover, .watch-actions a:hover, .watch-actions summary:hover, .watch-inline-actions button:hover { background: var(--surface-3); }
      .watch-actions .icon, .watch-inline-actions .icon { width: 16px; height: 16px; }
      .watch-actions button:disabled, .watch-inline-actions button:disabled { cursor: default; opacity: .6; }
      .watch-subscribe { min-height: 34px; border: 0; border-radius: 999px; display: inline-flex; align-items: center; gap: 7px; padding: 0 16px; cursor: pointer; font-size: 13px; font-weight: 600; background: #f1f1f3 !important; color: #0a0a0b !important; }
      .watch-subscribe .icon { width: 16px; height: 16px; }
      .watch-subscribe.subscribed { background: var(--surface-2) !important; color: var(--text) !important; font-weight: 500; }
      .watch-subscribe.subscribed:hover { background: var(--surface-2) !important; color: var(--text) !important; }
      .watch-reactions { display: inline-flex; align-items: center; background: var(--surface-2); border-radius: 999px; overflow: hidden; }
      .watch-reactions .watch-icon-action { border-radius: 0 !important; background: transparent !important; }
      .watch-reactions .watch-icon-action:first-child { border-right: 1px solid var(--border) !important; }
      .watch-icon-action { width: 42px; justify-content: center; padding: 0 !important; }
      .watch-icon-action.has-count { width: auto; padding: 0 14px !important; gap: 8px; }
      .watch-icon-action.is-selected { color: var(--text) !important; background: var(--surface-3) !important; }
      .reaction-count { font-size: 12.5px; font-variant-numeric: tabular-nums; color: var(--muted); }
      .watch-icon-action.is-selected .reaction-count { color: var(--text); }
      .reaction-count.muted { color: var(--muted-2); }

      /* ---------- theater mode ---------- */
      body.theater .watch-main { max-width: none; padding-left: 0; padding-right: 0; }
      body.theater .watch-layout { display: block; }
      body.theater .watch-player-shell { max-width: none; margin: 0 0 24px; border-radius: 0; border-left: 0; border-right: 0;
        max-height: min(80vh, calc(100vw * 9 / 16)); }
      body.theater .watch-player-shell.is-short { max-width: min(430px, 100%); margin-inline: auto; }
      body.theater .watch-content, body.theater .watch-panel { max-width: 1280px; margin-inline: auto; padding: 0 32px; }
      body.theater .watch-panel { margin-top: 32px; }
      body.nav-ready .watch-player-shell { transition: max-width .2s cubic-bezier(.2,.8,.3,1), border-radius .2s ease; }
      .theater-toggle .icon { transition: transform .18s ease; }
      .theater-toggle.is-selected .icon { transform: scaleX(1.15); }
      @media (max-width: 1100px) { body.theater .watch-content, body.theater .watch-panel { padding: 0 16px; } }

      /* ---------- comment replies ---------- */
      .reply-toggle { display: inline-flex; align-items: center; gap: 6px; min-height: 28px; margin-top: 6px; padding: 0 10px 0 8px;
        border: 0; border-radius: 999px; background: transparent; color: #6ea8ff; font-size: 12.5px; font-weight: 550; cursor: pointer; }
      .reply-toggle:hover { background: rgba(110,168,255,.12); }
      .reply-toggle .icon { width: 15px; height: 15px; transition: transform .16s cubic-bezier(.2,.8,.3,1); }
      .reply-toggle.is-open .icon { transform: rotate(180deg); }
      .comment-replies { display: grid; gap: 14px; margin: 10px 0 0 4px; padding-left: 14px; border-left: 1px solid var(--border-soft); }
      .comment-replies[hidden] { display: none; }
      .comment-replies .comment { gap: 10px; }
      .comment-replies .avatar { width: 26px; height: 26px; font-size: 9.5px; }
      @media (prefers-reduced-motion: reduce) { .reply-toggle .icon { transition: none; } }
      .watch-description { border: 1px solid var(--border-soft); border-radius: var(--r-panel); background: var(--surface); padding: 14px 16px; color: var(--text); line-height: 1.6; font-size: 13px; }
      .watch-description p { margin: 0; }
      .watch-description p + p { margin-top: 8px; }
      .watch-description-meta { color: var(--muted-2); font-size: 12px; font-weight: 500; margin-bottom: 10px !important; }
      .watch-description-body { overflow: hidden; }
      .watch-description.is-collapsed .watch-description-body { max-height: 4.4em; }
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
      .tab { flex: 0 0 auto; min-height: 30px; border: 0; border-radius: 8px; background: var(--surface-2); color: var(--text); cursor: pointer; padding: 0 12px; max-width: 160px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13px; }
      .tab:hover { background: var(--surface-3); }
      .tab.active { background: #f1f1f3; color: #0a0a0b; font-weight: 500; }
      .tab-panel { display: none; }
      .tab-panel.active { display: grid; gap: 6px; }
      .details-list { display: grid; grid-template-columns: 92px minmax(0, 1fr); gap: 10px; color: var(--text); font-size: 12.5px; border: 1px solid var(--border-soft); border-radius: 10px; background: var(--surface); padding: 14px; }
      .details-list dt { color: var(--muted-2); }
      .details-list dd { margin: 0; overflow-wrap: anywhere; }
      .chat-placeholder { color: var(--muted-2); line-height: 1.6; padding: 8px; font-size: 12.5px; }
      .empty-copy { padding: 18px; border: 1px solid var(--border-soft); border-radius: var(--r-panel); background: var(--surface); color: var(--muted); font-size: 13px; }
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
        .watch-layout { grid-template-columns: 1fr; }
        .watch-panel { position: static; max-height: none; overflow: visible; }
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
        .watch-actions { overflow-x: auto; flex-wrap: nowrap; padding-bottom: 4px; }
        .watch-panel .media-row { grid-template-columns: minmax(132px, 42%) minmax(0, 1fr) 22px; }
        .mobile-bottom-nav { position: fixed; left: 0; right: 0; bottom: 0; z-index: 80; height: calc(56px + env(safe-area-inset-bottom)); padding-bottom: env(safe-area-inset-bottom); background: rgba(10,10,11,.94); backdrop-filter: blur(12px); border-top: 1px solid var(--border-soft); display: grid; grid-template-columns: repeat(4, 1fr); }
        .mobile-nav-item { display: grid; place-items: center; align-content: center; gap: 4px; color: var(--muted); font-size: 9.5px; min-width: 0; }
        .mobile-nav-item .icon { width: 19px; height: 19px; }
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
    return '''
      const PLAYLIST_ICON = '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 6h12"/><path d="M4 12h12"/><path d="M4 18h7"/><path d="M18 15v6"/><path d="M15 18h6"/></svg>';
      const LIKE_ICON = '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M7 10v12"/><path d="M15 5.88 14 10h5.83a2 2 0 0 1 1.92 2.56l-2.33 8A2 2 0 0 1 17.5 22H4a2 2 0 0 1-2-2v-8a2 2 0 0 1 2-2h2.76a2 2 0 0 0 1.79-1.11L12 2a3.13 3.13 0 0 1 3 3.88"/></svg>';
      const PLUS_ICON = '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14"/><path d="M5 12h14"/></svg>';
      const CHECK_ICON = '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="m5 12 4 4L19 6"/></svg>';
      const COLLECTIONS_ICON = '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="4" width="7" height="7" rx="2"/><rect x="14" y="4" width="7" height="7" rx="2"/><rect x="3" y="13" width="7" height="7" rx="2"/><rect x="14" y="13" width="7" height="7" rx="2"/></svg>';
      const CHEVRON_ICON = '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg>';
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
          button.classList.toggle('is-selected', selected);
          button.setAttribute('aria-pressed', selected ? 'true' : 'false');
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
            feedContainer.innerHTML = html || '<p class="empty-copy">No videos match this filter yet.</p>';
          } else if (html) {
            feedContainer.insertAdjacentHTML('beforeend', html);
          }
          feedContainer.dataset.nextPage = String(data.next_page || page + 1);
          feedContainer.dataset.feedFilter = filter;
          feedHasMore = Boolean(data.has_more && (html || options.reset));
        } catch (error) {
          if (retryBox) retryBox.hidden = false;
          if (options.reset && feedContainer && !feedContainer.children.length) {
            feedContainer.innerHTML = '<p class="empty-copy">Could not load recommendations.</p>';
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

      /* Unsubscribing only deletes the FreshRSS feed row: the channel's videos are
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
            timeline.outerHTML = '<p class="empty-copy">No videos from your subscriptions yet. Run a FreshRSS sync to pull them in.</p>';
          }
          /* Backfill the home feed so removed cards do not leave a short page. */
          if (removed && feedContainer && feedHasMore) loadFeedPage();
        }, 260);
      }

      document.addEventListener('click', async (event) => {
        const summary = event.target.closest('summary');
        if (summary && summary.closest('.yt-menu')) {
          const current = summary.closest('.yt-menu');
          closeMenus(current);
          window.requestAnimationFrame(() => positionMenu(current));
          event.stopPropagation();
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
              showToast('Subscribed in FreshRSS');
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
          empty.textContent = 'No FreshRSS subscriptions imported yet.';
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
        tuneRoot.style.setProperty('--tune-hue', String(hue));

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

        const slides = () => [...stage.querySelectorAll('[data-shorts-slide]')];

        // Only the visible Short gets an iframe; the rest fall back to their poster so we
        // are never running a dozen players at once.
        function mountPlayer(slide) {
          const frame = slide.querySelector('[data-shorts-frame]');
          if (!frame || frame.querySelector('iframe')) return;
          const videoId = slide.dataset.videoId;
          const iframe = document.createElement('iframe');
          iframe.src = `/watch/${encodeURIComponent(videoId)}/player`;
          iframe.title = slide.dataset.title || 'Short';
          iframe.allow = 'autoplay; fullscreen; picture-in-picture';
          iframe.setAttribute('allowfullscreen', '');
          frame.appendChild(iframe);
          slide.classList.add('is-playing');
        }

        function unmountPlayer(slide) {
          const frame = slide.querySelector('[data-shorts-frame]');
          frame?.querySelector('iframe')?.remove();
          slide.classList.remove('is-playing');
        }

        function setActive(slide) {
          if (!slide || slide === activeSlide) return;
          if (activeSlide) unmountPlayer(activeSlide);
          activeSlide = slide;
          mountPlayer(slide);
          if (panel && !panel.hidden) loadShortComments(slide.dataset.videoId);
          const url = `/shorts?start=${encodeURIComponent(slide.dataset.videoId)}`;
          history.replaceState(null, '', url);
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
        body.textContent = item.content || '';

        const meta = document.createElement('div');
        meta.className = 'comment-meta';
        const likes = document.createElement('span');
        likes.innerHTML = LIKE_ICON;
        likes.append(' ' + formatCount(item.like_count || 0));
        meta.appendChild(likes);

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
              if (!replies.length) {
                const empty = document.createElement('p');
                empty.className = 'comments-state';
                empty.textContent = 'No replies to show.';
                thread.appendChild(empty);
                return;
              }
              replies.forEach((reply) => thread.appendChild(renderComment(reply)));
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
        return row;
      }

      function formatCount(value) {
        const n = Number(value) || 0;
        if (n >= 1000000) return (n / 1000000).toFixed(n >= 10000000 ? 0 : 1).replace(/\\.0$/, '') + 'M';
        if (n >= 1000) return (n / 1000).toFixed(n >= 10000 ? 0 : 1).replace(/\\.0$/, '') + 'K';
        return String(n);
      }

      async function loadComments() {
        const section = document.querySelector('[data-comments-for]');
        if (!section) return;
        const videoId = section.dataset.commentsFor;
        const body = section.querySelector('[data-comments-body]');
        const counter = section.querySelector('[data-comments-count]');
        try {
          const response = await fetch(`/api/comments/${encodeURIComponent(videoId)}`);
          if (!response.ok) throw new Error(String(response.status));
          const data = await response.json();
          const items = data.items || [];
          body.replaceChildren();
          if (counter && data.comment_count != null) counter.textContent = formatCount(data.comment_count);
          if (!items.length) {
            const empty = document.createElement('p');
            empty.className = 'comments-state';
            empty.textContent = 'No comments on this video.';
            body.appendChild(empty);
            return;
          }
          items.forEach((item) => body.appendChild(renderComment(item)));
        } catch (error) {
          body.replaceChildren();
          const failed = document.createElement('p');
          failed.className = 'comments-state';
          failed.textContent = 'Comments could not be loaded right now.';
          body.appendChild(failed);
        }
      }
      loadComments();

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
      document.addEventListener('click', async (event) => {
        const button = event.target.closest('[data-download-video]');
        if (!button) return;
        event.preventDefault();
        const videoId = button.dataset.downloadVideo;
        if (!videoId) return;
        button.disabled = true;
        showToast('Preparing download\\u2026');
        try {
          const info = await fetch(`/api/download/${encodeURIComponent(videoId)}/info`).then((r) => r.json());
          if (!info.ok) throw new Error(info.detail || 'unavailable');
          /* A plain anchor click keeps the browser's own download UI and resume support. */
          const link = document.createElement('a');
          link.href = `/api/download/${encodeURIComponent(videoId)}`;
          link.download = info.filename || '';
          document.body.appendChild(link);
          link.click();
          link.remove();
          showToast(info.quality ? `Downloading ${info.quality}` : 'Download started');
        } catch (error) {
          showToast('No downloadable stream for this video');
        } finally {
          button.disabled = false;
        }
      });

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
            <a class="brand" href="/" aria-label="OpenFeed home"><span class="brand-mark">Open</span><span>Feed</span></a>
          </div>
          <div class="header-center">
            <form class="search-form" action="/search" method="get">
              <input name="q" type="search" value="{escape(search_value, quote=True)}" placeholder="Search" aria-label="Search imported videos">
              <button type="submit" aria-label="Search">{_icon("search")}</button>
            </form>
            <a class="header-mic" href="/search" aria-label="Search by voice is unavailable; open search">{_icon("mic")}</a>
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


def _asset_preconnect() -> str:
    """Warm the TLS handshakes for thumbnails and the fallback player up front."""
    hints = ['<link rel="preconnect" href="https://i.ytimg.com" crossorigin>']
    if get_settings().youtube_fallback_enabled:
        hints.append('<link rel="preconnect" href="https://www.youtube-nocookie.com" crossorigin>')
    return "".join(hints)


def _app_shell(title: str, active: str, content: str, search_value: str = "", main_class: str = "app-page", scripts: str = "", request: Request | None = None, immersive: bool = False) -> str:
    settings = get_settings()
    user = _current_user(request)
    user_key = _user_key(request)
    configured_text = "FreshRSS connected" if settings.freshrss_configured else "FreshRSS not configured"
    meta_html = f'<p>{escape(configured_text)}</p><p>Private video library<br>Invidious-backed playback</p>'
    return f'''
    <!doctype html>
    <html lang="en">
      <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>{escape(title)} - OpenFeed</title>
        {_asset_preconnect()}
        <style>{_home_styles()}</style>
      </head>
      <body{' class="immersive-nav"' if immersive else ''}>
        {_app_header(search_value, user, user_key)}
        {_app_sidebar(active, meta_html)}
        <div class="nav-scrim" data-nav-scrim aria-hidden="true"></div>
        <main class="home-main {escape(main_class)}">{content}</main>
        <nav class="mobile-bottom-nav" aria-label="Mobile navigation">{_mobile_nav()}</nav>
        {_settings_dialog(user, user_key)}
        {_playlist_dialogs()}
        {_collection_dialogs()}
        <div class="feedback-toast" data-feedback-toast role="status" aria-live="polite"></div>
        <script>{_home_script()}</script>
        {scripts}
      </body>
    </html>
    '''


def _media_row(video: dict[str, Any], settings: Any, label: str = "") -> str:
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
    return f'''
    <article class="media-row" data-video-card data-video-id="{video_id}" data-channel-id="{channel_id}">
      <a class="thumb" href="{watch_href}" aria-label="Watch {title}"><span class="thumb-fallback">OpenFeed</span>{thumb_img}{duration_html}</a>
      <div class="media-row-copy">
        <h2><a href="{watch_href}">{title}</a></h2>
        <p><a href="{channel_href}">{channel}</a></p>
        <p>{meta}</p>
        <span class="row-label">{source_label}</span>
      </div>
      {menu}
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


def _subscription_card(subscription: dict[str, Any]) -> str:
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
    feed_label = "Playlist" if _is_playlist_subscription(subscription) else "FreshRSS feed"
    hue = _avatar_hue(title_text)
    initials = escape(_avatar_initials(title_text))
    feed_url_raw = str(subscription.get("feed_url") or "")
    subscribe_pill = ""
    if channel_id_raw and feed_url_raw:
        subscribe_pill = (
            f'<button class="subscribe-pill subscribed" type="button" data-unsubscribe-feed '
            f'data-channel-id="{channel_id}" data-feed-url="{escape(feed_url_raw, quote=True)}" '
            f'data-title="{escape(title_text, quote=True)}">{_icon("bell")}<span>Subscribed</span></button>'
        )
    subscription_id = escape(str(subscription.get("id") or ""), quote=True)
    return f'''
    <article class="channel-card" data-subscription-card="{subscription_id}" data-channel-id="{channel_id}" data-filter-item="{escape(title_text, quote=True)}">
      <a class="channel-card-top" href="{href}"{rel}>
        {_avatar_html(title_text, subscription.get("channel_avatar_url"))}
        <div>
          <h2>{title}</h2>
          <p>{escape(feed_label)} &middot; {video_count} videos</p>
          <p>{latest_html}</p>
        </div>
      </a>
      <div class="card-actions-inline">{subscribe_pill}</div>
    </article>
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
        subscription_id = escape(str(member.get("id") or ""), quote=True)
        title_text = str(member.get("title") or "Untitled subscription")
        card = _subscription_card(member)
        remove = (
            f'<button class="collection-remove" type="button" data-remove-collection-member="{subscription_id}" '
            f'data-collection-id="{collection_id_attr}" aria-label="Remove {escape(title_text)} from {name}">{_icon("x")}</button>'
        )
        card = card.replace('<article class="channel-card"', f'<article class="channel-card" data-member-card="{subscription_id}"', 1)
        card = card.replace("</article>", f"{remove}</article>", 1)
        member_cards.append(card)
    members_html = "".join(member_cards) or '<p class="empty-copy">No channels in this collection yet.</p>'
    videos_html = "".join(_home_video_card(video, index, settings) for index, video in enumerate(videos, start=1)) or '<p class="empty-copy">No videos from these channels yet. Run a FreshRSS sync to pull them in.</p>'
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
        initial_sections = '<p class="empty-copy">No recommendations yet. Run a FreshRSS sync, then refresh this page.</p>'
    creator_strip = _creator_strip(all_for_channels)
    result_count = len(recommendations_preview)
    library_count = len(library_videos)
    configured_text = "FreshRSS connected" if settings.freshrss_configured else "FreshRSS not configured"

    return f'''
    <!doctype html>
    <html lang="en">
      <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>OpenFeed</title>
        {_asset_preconnect()}
        <style>{_home_styles()}</style>
      </head>
      <body>
        {_app_header("", user, user_key)}
        {_app_sidebar("home", f'<p>{escape(configured_text)}</p><p>{result_count} recommendations<br>{library_count} library videos</p>')}
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


@app.get("/recommendations", response_class=HTMLResponse)
def recommendations_page(request: Request):
    return index(request)


@app.get("/watch/{youtube_video_id}", response_class=HTMLResponse)
def watch_video(request: Request, youtube_video_id: str):
    auth_redirect = _login_required_redirect(request)
    if auth_redirect:
        return auth_redirect
    user_key = _user_key(request)
    video = db.get_video_by_youtube_id(youtube_video_id)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    if video.get("view_count") is None and not video.get("metadata_error"):
        enrich_video(youtube_video_id, video.get("id"))
        video = db.get_video_by_youtube_id(youtube_video_id) or video

    settings = get_settings()
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
    summary_text = _text_excerpt(video.get("summary") or video.get("description") or "", 2400)
    summary = escape(summary_text)
    published_relative = _relative_time(video.get("published_at"))
    published_date = _date_text(video.get("published_at"))
    published_text = escape(published_relative or published_date)
    published_detail = escape(published_date or published_relative or "Release date unavailable")
    views = _compact_count(video.get("view_count"))
    view_text = f"{escape(views)} views" if views else "Views unavailable"
    subscribers = _compact_count(video.get("channel_subscriber_count"))
    subscriber_text = f"{escape(subscribers)} subscribers" if subscribers else ""
    description_meta = " &middot; ".join(part for part in [view_text, f"Released {published_detail}" if published_detail else ""] if part)
    subscription = escape(video.get("subscription_title") or _source_label(video))
    read_state = "read" if video.get("is_read") else "unread"
    starred_state = "starred" if video.get("is_starred") else "not starred"
    metadata_source = escape(video.get("metadata_source") or "not fetched")
    duration_text = escape(_duration_text(video.get("duration_seconds")) or "unknown")
    captions_status = escape(video.get("captions_status") or "unknown")
    initials = escape(_avatar_initials(channel_text))
    hue = _avatar_hue(channel_text)
    subscription_feed_url_raw = str(video.get("subscription_feed_url") or "").strip()
    feed_url_raw = subscription_feed_url_raw or (_youtube_feed_url(channel_id_raw) if channel_id_raw else "")
    feed_url = escape(feed_url_raw, quote=True)
    subscribed = bool(subscription_feed_url_raw)
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
    dislike_count_html = (
        f'<span class="reaction-count" data-dislike-count>{escape(dislike_total)}</span>'
        if dislike_total
        else '<span class="reaction-count muted" title="YouTube stopped publishing dislike counts in 2021">&mdash;</span>'
    )
    metadata = video.get("metadata") if isinstance(video.get("metadata"), dict) else {}
    transcript_segments = metadata.get("transcript_segments") if isinstance(metadata, dict) else None
    has_transcript = isinstance(transcript_segments, list) and bool(transcript_segments)
    db.record_video_event(youtube_video_id, "watch_page_open", {"source": "watch_page", "weight": 0}, user_key)
    db.update_playback_progress(youtube_video_id, 0, video.get("duration_seconds"), "open", {"source": "watch_page"}, user_key)
    _sync_gorse_event(user_key, youtube_video_id, "watch_page_open", {"source": "watch_page"})

    suggestion_items = []
    seen_suggestions = {youtube_video_id}
    for candidate in db.list_recommended_videos(32, 0, "all", user_key) + db.list_subscription_recommendations(12, user_key):
        candidate_id = str(candidate.get("youtube_video_id") or "")
        if not candidate_id or candidate_id in seen_suggestions:
            continue
        seen_suggestions.add(candidate_id)
        suggestion_items.append(_media_row(candidate, settings, _source_label(candidate)))
        if len(suggestion_items) >= 14:
            break
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
    description_html = f'''<section class="watch-description is-collapsed" data-description-box aria-label="Video description">
            <p class="watch-description-meta">{description_meta}</p>
            <div class="watch-description-body">{description_body}</div>
            <button class="description-toggle" type="button" data-description-toggle data-more-label="More" data-less-label="Show less">More</button>
          </section>'''
    subscribe_button = ""
    if channel_id_raw and feed_url_raw:
        if subscribed:
            subscribe_button = f'''<button class="watch-subscribe subscribed" type="button" data-unsubscribe-feed data-channel-id="{channel_id}" data-feed-url="{feed_url}" data-title="{escape(channel_text, quote=True)}" aria-label="Unsubscribe from {channel}">{_icon("check")}<span>Subscribed</span></button>'''
        else:
            subscribe_button = f'''<button class="watch-subscribe" type="button" data-subscribe-feed data-channel-id="{channel_id}" data-feed-url="{feed_url}" data-title="{escape(channel_text, quote=True)}" aria-label="Subscribe to {channel}">{_icon("channels")}<span>Subscribe</span></button>'''

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
      <div class="watch-layout">
        <article class="watch-content">
          <div class="watch-player-shell{short_class}">
            <iframe class="player-frame" src="/watch/{escape(youtube_video_id)}/player" title="Invidious player" allow="autoplay; fullscreen; picture-in-picture" allowfullscreen></iframe>
          </div>
          <h1 class="watch-title">{title}</h1>
          <div class="watch-meta-line">
            <div class="watch-channel">
              {_avatar_html(channel_text, video.get("channel_avatar_url"), tag="a", href=channel_href)}
              <div>
                <a class="watch-channel-name" href="{channel_href}">{channel}</a>
                <p class="metadata">{subscriber_text or subscription}</p>
                <p class="metadata">{view_text}{f' &middot; {published_text}' if published_text else ''}</p>
              </div>
            </div>
            <div class="watch-inline-actions">{subscribe_button}</div>
          </div>
          <div class="watch-actions" aria-label="Video actions">
            <div class="watch-reactions">
              <button class="watch-icon-action has-count{like_selected}" type="button" data-feedback="like" data-video-id="{escape(youtube_video_id)}" aria-label="Like{f' ({like_total} likes)' if like_total else ''}" title="Like" aria-pressed="{like_pressed}">{_icon("like")}{like_count_html}<span class="sr-only">Like</span></button>
              <button class="watch-icon-action has-count{dislike_selected}" type="button" data-feedback="dislike" data-video-id="{escape(youtube_video_id)}" aria-label="Dislike" title="Dislike" aria-pressed="{dislike_pressed}">{_icon("dislike")}{dislike_count_html}<span class="sr-only">Dislike</span></button>
            </div>
            <button type="button" data-copy-url="/watch/{escape(youtube_video_id)}" aria-label="Copy share link" title="Share">{_icon("share")}<span>Share</span></button>
            <button type="button" data-save-to-playlist="{escape(youtube_video_id)}" data-video-title="{escape(_text_excerpt(title_text, 90), quote=True)}">{_icon("playlist")}<span>Save</span></button>
            <button type="button" data-download-video="{escape(youtube_video_id)}" data-video-title="{escape(_text_excerpt(title_text, 90), quote=True)}">{_icon("download")}<span>Download</span></button>
            <button class="theater-toggle" type="button" data-theater-toggle aria-pressed="false" aria-label="Toggle theater mode" title="Theater mode (t)">{_icon("theater")}<span>Theater</span></button>
            <button type="button" data-feedback="mark_watched" data-video-id="{escape(youtube_video_id)}">{_icon("check")}<span>Watched</span></button>
            {_watch_options_menu(youtube_video_id, invidious_url, youtube_url)}
          </div>
          {description_html}
          {comments_html}
        </article>
        <aside class="watch-panel" aria-label="Watch side panel">
          {f'<div class="tabbar" role="tablist" aria-label="Watch panels"><button class="tab active" type="button" data-tab="suggestions">Up next</button>{transcript_button}</div>' if transcript_button else '<div class="section-heading"><h2>Up next</h2></div>'}
          <section id="tab-suggestions" class="tab-panel active">{suggestions_html}</section>
          {transcript_panel}
        </aside>
      </div>
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
    return ok, reason


def _all_playback_sources(user_key: str = "local") -> list[dict[str, Any]]:
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
    return sources


def _configured_playback_sources(user_key: str = "local") -> list[dict[str, Any]]:
    """The enabled sources only, in preference order, before health is applied."""
    return [source for source in _all_playback_sources(user_key) if source.get("enabled", True)]


def _source_url(source: dict[str, str], video_id: str, play: str) -> str:
    if source["kind"] == "invidious-local":
        return f"/embed/{video_id}?autoplay={play}"
    if source["kind"] == "invidious-public":
        return f"{source['base']}/embed/{video_id}?autoplay={play}"
    return f"https://www.youtube-nocookie.com/embed/{video_id}?autoplay={play}&rel=0&modestbranding=1&playsinline=1"


def _playback_sources(youtube_video_id: str, autoplay: bool = True, user_key: str = "local") -> tuple[list[dict[str, str]], str]:
    """The ordered list of players to try for one video.

    Order is the user's preference, re-sorted so sources currently in cooldown sink to
    the bottom instead of being dropped: a cooled-down source is still better than no
    player at all, and trying it again is exactly how it gets marked healthy once more.
    """
    video_id = urllib.parse.quote(youtube_video_id, safe="")
    play = "1" if autoplay else "0"
    configured = _configured_playback_sources(user_key)
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
            "url": _source_url(source, video_id, play),
            "same_origin": "1" if source["kind"] == "invidious-local" else "",
        })
    return ordered, reason


@app.get("/watch/{youtube_video_id}/player", response_class=HTMLResponse)
def watch_player(request: Request, youtube_video_id: str):
    auth_redirect = _login_required_redirect(request)
    if auth_redirect:
        return auth_redirect
    video_id = escape(youtube_video_id)
    public = get_settings().invidious_public_url
    invidious_url = f"{public}/watch?v={video_id}" if public else ""
    youtube_url = f"https://www.youtube.com/watch?v={video_id}"
    sources, reason = _playback_sources(youtube_video_id, user_key=_user_key(request))
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
          <img class="poster" src="https://i.ytimg.com/vi/{video_id}/hqdefault.jpg" alt="" aria-hidden="true">
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
            noteLabel.textContent = preferred ? '' : `Playing via ${source.label}${why ? ' — ' + why : ''}`;
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

        function watchSameOrigin() {
          let doc = null;
          try { doc = frame.contentDocument; } catch (error) { doc = null; }
          if (!doc) return false;
          const video = doc.querySelector('video');
          if (!video) { next('no video element'); return true; }
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
        with httpx.Client(timeout=20, follow_redirects=True) as client:
            upstream = client.request(request.method, url, headers=_invidious_proxy_headers(request))
    except Exception as exc:
        return Response(status_code=502, headers={"cache-control": "no-store"}, content=str(exc)[:200])

    content_type = (upstream.headers.get("content-type") or "").lower()
    body = upstream.content if request.method != "HEAD" else b""
    if upstream.status_code != 200 or not content_type.startswith("image/") or (request.method != "HEAD" and not body):
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
def search_page(request: Request, q: str = "", limit: int = 40):
    auth_redirect = _login_required_redirect(request)
    if auth_redirect:
        return auth_redirect
    settings = get_settings()
    cleaned = q.strip()
    videos = db.search_videos(cleaned, max(1, min(limit, 100)))
    rows = "".join(_media_row(video, settings, "Search result") for video in videos)
    if not rows:
        rows = '<p class="empty-copy">No matching videos.</p>'
    heading = f'Results for "{escape(cleaned)}"' if cleaned else "Recent videos"
    helper = "Use the search bar in the header to refine this feed." if cleaned else "Use the search bar in the header to search OpenFeed."
    content = f'''
      <section class="page-hero search-summary">
        <div>
          <p class="page-kicker">Search</p>
          <h1>{heading}</h1>
          <p>{helper}</p>
        </div>
      </section>
      <section class="result-list">{rows}</section>
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
        return '<p class="empty-copy">No videos from your subscriptions yet. Run a FreshRSS sync to pull them in.</p>'
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
        subscription_cards = '<p class="empty-copy">No FreshRSS subscriptions imported yet.</p>'

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
    return _app_shell("Subscriptions", "channels", content, request=request)


@app.get("/library", response_class=HTMLResponse)
def library_page(request: Request, limit: int = 80):
    auth_redirect = _login_required_redirect(request)
    if auth_redirect:
        return auth_redirect
    settings = get_settings()
    user_key = _user_key(request)
    bounded_limit = max(1, min(limit, 120))
    freshrss_playlists = db.list_playlist_subscriptions(bounded_limit)
    custom_playlists = db.list_playlists(user_key)
    liked = db.list_liked_videos(bounded_limit, user_key)
    history = db.list_history_videos(bounded_limit, user_key)

    playlist_cards = "".join(_playlist_card(playlist) for playlist in custom_playlists)
    if not playlist_cards:
        playlist_cards = '<p class="empty-copy">No playlists yet. Create one, then use “Save to playlist” on any video.</p>'
    freshrss_section = ""
    if freshrss_playlists:
        freshrss_section = f'''
          <section class="section-block">
            <div class="section-heading"><h2>Playlist feeds from FreshRSS</h2></div>
            <div class="channel-grid">{"".join(_subscription_card(item) for item in freshrss_playlists)}</div>
          </section>
        '''
    liked_grid = "".join(_home_video_card(video, index, settings) for index, video in enumerate(liked[:36], start=1))
    if not liked_grid:
        liked_grid = '<p class="empty-copy">No liked or starred videos yet. Like a video, or star it in FreshRSS.</p>'

    history_groups: list[tuple[str, list[dict[str, Any]]]] = []
    for video in history[:60]:
        label = _timeline_day_label(video.get("latest_event_at") or video.get("published_at"))
        if history_groups and history_groups[-1][0] == label:
            history_groups[-1][1].append(video)
        else:
            history_groups.append((label, [video]))
    if history_groups:
        blocks = []
        for index, (label, items) in enumerate(history_groups):
            rows = "".join(_media_row(video, settings, "History") for video in items)
            group_class = "timeline-group" if index == 0 else "timeline-group past"
            blocks.append(f'<section class="{group_class}"><p class="timeline-date">{escape(label)}</p><div class="library-list">{rows}</div></section>')
        history_html = f'<div class="timeline">{"".join(blocks)}</div>'
    else:
        history_html = '<p class="empty-copy">No watch history recorded yet.</p>'

    content = f'''
      <div class="page-head">
        <div>
          <h1>Library</h1>
          <p>Playlists, liked videos, and watch history from your private OpenFeed activity.</p>
        </div>
        <div class="page-actions">
          <button class="primary-action" type="button" data-new-playlist>{_icon("plus")}<span>New playlist</span></button>
        </div>
      </div>

      <div class="page-tabs" role="tablist" aria-label="Library views">
        <button class="page-tab active" type="button" data-page-tab="playlists" role="tab">Playlists <span class="count">{len(custom_playlists)}</span></button>
        <button class="page-tab" type="button" data-page-tab="liked" role="tab">Liked <span class="count">{len(liked)}</span></button>
        <button class="page-tab" type="button" data-page-tab="history" role="tab">History <span class="count">{len(history)}</span></button>
      </div>

      <section data-tab-panel="playlists" id="playlists">
        <div class="toolbar">
          <span class="toolbar-note">{_icon("playlist")}<span>{len(custom_playlists)} playlist{"" if len(custom_playlists) == 1 else "s"}</span></span>
          <label class="toolbar-search">{_icon("search")}<input type="search" placeholder="Search playlists" data-filter-input aria-label="Filter playlists"></label>
        </div>
        <div class="playlist-grid">{playlist_cards}</div>
        {freshrss_section}
      </section>

      <section data-tab-panel="liked" id="liked" hidden>
        <div class="toolbar"><span class="toolbar-note">{_icon("like")}<span>{len(liked)} saved video{"" if len(liked) == 1 else "s"}</span></span></div>
        <div class="video-grid">{liked_grid}</div>
      </section>

      <section data-tab-panel="history" id="history" hidden>
        <div class="toolbar"><span class="toolbar-note">{_icon("history")}<span>{len(history)} item{"" if len(history) == 1 else "s"}</span></span></div>
        {history_html}
      </section>
    '''
    return _app_shell("Library", "library", content, request=request)


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
        video_id = escape(str(video.get("youtube_video_id") or ""))
        row = _media_row(video, settings)
        remove = (
            f'<button class="playlist-remove" type="button" data-remove-playlist-item="{video_id}" '
            f'data-playlist-id="{playlist_id_attr}" aria-label="Remove from playlist">{_icon("x")}</button>'
        )
        row = row.replace('<article class="media-row">', f'<article class="media-row" data-playlist-row="{video_id}"><span class="row-index">{index}</span>', 1)
        row = row.replace("</article>", f"{remove}</article>", 1)
        rows.append(row)
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
            <button class="text-action" type="button" data-delete-playlist="{playlist_id_attr}" data-redirect="/library">{_icon("trash")}<span>Delete</span></button>
          </div>
        </aside>
        <section class="playlist-items">{items_html}</section>
      </div>
    '''
    return _app_shell(name_text, "library", content, request=request)


@app.get("/channel/{youtube_channel_id}", response_class=HTMLResponse)
def channel_page(request: Request, youtube_channel_id: str, limit: int = 80, sort: str = "latest"):
    auth_redirect = _login_required_redirect(request)
    if auth_redirect:
        return auth_redirect
    channel = db.get_channel_by_youtube_id(youtube_channel_id)
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")

    settings = get_settings()
    sort_key = sort if sort in {"latest", "popular", "oldest"} else "latest"
    bounded_limit = max(1, min(limit, 500))
    videos = db.list_channel_videos(youtube_channel_id, bounded_limit, sort_key)
    title_text = str(channel.get("title") or "Unknown channel")
    title = escape(title_text)
    video_count = int(channel.get("video_count") or len(videos))
    latest = _relative_time(channel.get("latest_video_at"))
    initials = escape(_avatar_initials(title_text))
    hue = _avatar_hue(title_text)
    subscription_link = escape(channel.get("subscription_html_url") or "")
    feed_url_raw = str(channel.get("feed_url") or _youtube_feed_url(youtube_channel_id))
    subscribed = bool(channel.get("subscription_id"))
    subscribe_button = ""
    if youtube_channel_id and feed_url_raw:
        feed_url = escape(feed_url_raw, quote=True)
        if subscribed:
            subscribe_button = f'<button class="watch-subscribe subscribed" type="button" data-unsubscribe-feed data-channel-id="{escape(youtube_channel_id, quote=True)}" data-feed-url="{feed_url}" data-title="{escape(title_text, quote=True)}">{_icon("check")}<span>Subscribed</span></button>'
        else:
            subscribe_button = f'<button class="watch-subscribe" type="button" data-subscribe-feed data-channel-id="{escape(youtube_channel_id, quote=True)}" data-feed-url="{feed_url}" data-title="{escape(title_text, quote=True)}">{_icon("channels")}<span>Subscribe</span></button>'
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
        video_grid = '<p class="empty-copy">No videos imported for this channel yet.</p>'
    external = f'<a class="text-action" href="{subscription_link}" rel="noreferrer">{_icon("external")}<span>Original</span></a>' if subscription_link else ''
    tab_html = "".join(
        f'<a class="page-tab{" active" if sort_key == key else ""}" href="/channel/{escape(youtube_channel_id)}?sort={key}&limit={bounded_limit}"{" aria-current=\"page\"" if sort_key == key else ""}>{label}</a>'
        for key, label in (("latest", "Latest"), ("popular", "Popular"), ("oldest", "Oldest"))
    )
    more_link = ""
    if len(videos) >= bounded_limit and bounded_limit < 500:
        next_limit = min(500, bounded_limit + 80)
        more_link = f'<a class="text-action" href="/channel/{escape(youtube_channel_id)}?sort={sort_key}&limit={next_limit}">{_icon("playlist")}<span>Load older</span></a>'
    subscriber_count = _compact_count(channel.get("subscriber_count"))
    meta_parts = [f"{video_count} imported videos"]
    if subscriber_count:
        meta_parts.insert(0, f"{subscriber_count} subscribers")
    if latest:
        meta_parts.append(f"latest {latest}")
    channel_meta = escape(" · ".join(meta_parts))
    content = f'''
      {banner_html}
      <section class="channel-hero">
        {_avatar_html(title_text, channel.get("avatar_url"), css_class="avatar channel-hero-avatar")}
        <div>
          <h1>{title} {_icon("verified").replace('class="icon"', 'class="icon verified"')}</h1>
          <p class="channel-hero-meta">{channel_meta}</p>
          <div class="channel-hero-actions">{subscribe_button}{collection_button}<a class="text-action" href="/channels">{_icon("subs")}<span>All channels</span></a>{external}{more_link}</div>
        </div>
      </section>
      <div class="page-tabs" aria-label="Channel video sorting">{tab_html}</div>
      <section class="video-grid">{video_grid}</section>
    '''
    return _app_shell(title_text, "channels", content, request=request)


@app.get("/api/videos")
def api_videos(limit: int = 50) -> dict[str, Any]:
    return {"items": _json_ready(db.list_recent_videos(max(1, min(limit, 200))))}


@app.get("/api/search")
def api_search(q: str = "", limit: int = 50) -> dict[str, Any]:
    return {"items": _json_ready(db.search_videos(q, max(1, min(limit, 200))))}


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


@app.get("/api/feed/preferences")
def api_feed_preferences(request: Request) -> dict[str, Any]:
    return {"ok": True, "preferences": _json_ready_value(db.get_feed_preferences(_user_key(request)))}


@app.post("/api/feed/preferences")
def api_update_feed_preferences(request: Request, payload: FeedPreferencesRequest) -> dict[str, Any]:
    preferences = db.set_feed_preferences(
        preferred_topics=payload.preferred_topics,
        muted_topics=payload.muted_topics,
        prioritize_subscriptions=payload.prioritize_subscriptions,
        show_comments=payload.show_comments,
        exclude_shorts=payload.exclude_shorts,
        discovery_level=payload.discovery_level,
        user_key=_user_key(request),
    )
    return {"ok": True, "preferences": _json_ready_value(preferences)}


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


@app.post("/api/subscriptions/freshrss")
def api_subscribe_freshrss(payload: SubscribeRequest) -> dict[str, Any]:
    settings = get_settings()
    if not settings.freshrss_configured:
        raise HTTPException(status_code=409, detail="FreshRSS is not configured")

    youtube_channel_id = payload.youtube_channel_id.strip()
    feed_url = payload.feed_url.strip() or (_youtube_feed_url(youtube_channel_id) if youtube_channel_id else "")
    if not feed_url:
        raise HTTPException(status_code=400, detail="A feed URL or YouTube channel ID is required")

    title = payload.title.strip() or youtube_channel_id or feed_url
    client = FreshRSSClient(settings)
    try:
        client.subscribe_feed(feed_url, title)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"FreshRSS subscribe failed: {exc}") from exc

    # Ask FreshRSS for the id it just assigned. Storing a "feed/<url>" placeholder instead
    # would collide with the real "feed/<n>" row the next sync imports, listing the
    # channel twice.
    stream_id = client.stream_id_for_feed_url(feed_url) or f"feed/{feed_url}"
    html_url = f"https://www.youtube.com/channel/{youtube_channel_id}" if youtube_channel_id else None
    db.upsert_subscription(
        {
            "id": stream_id,
            "title": title,
            "url": feed_url,
            "htmlUrl": html_url,
            "categories": [],
        },
        youtube_channel_id or None,
    )
    if youtube_channel_id:
        db.upsert_channel(title, youtube_channel_id, feed_url)
    return {"ok": True, "feed_url": feed_url, "youtube_channel_id": youtube_channel_id or None, "title": title}


@app.delete("/api/subscriptions/freshrss")
def api_unsubscribe_freshrss(payload: SubscribeRequest) -> dict[str, Any]:
    settings = get_settings()
    if not settings.freshrss_configured:
        raise HTTPException(status_code=409, detail="FreshRSS is not configured")

    youtube_channel_id = payload.youtube_channel_id.strip()
    feed_url = payload.feed_url.strip() or (_youtube_feed_url(youtube_channel_id) if youtube_channel_id else "")
    if not feed_url:
        raise HTTPException(status_code=400, detail="A feed URL or YouTube channel ID is required")

    try:
        FreshRSSClient(settings).unsubscribe_feed(feed_url)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"FreshRSS unsubscribe failed: {exc}") from exc

    removed = db.remove_subscription(feed_url, youtube_channel_id or None)
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


@app.delete("/api/subscription-collections/{collection_id}/members/{subscription_id:path}")
def api_remove_subscription_collection_member(request: Request, collection_id: str, subscription_id: str) -> dict[str, Any]:
    # FreshRSS subscription ids contain a slash ("feed/4"), so this segment must be a path param.
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
    gorse_state = _sync_gorse_event(user_key, youtube_video_id, event_type, feedback.payload)
    return {"ok": True, "event": _json_ready_value(event), "feedback": _json_ready_value(state), "gorse": _json_ready_value(gorse_state)}


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
    )
    if not state:
        raise HTTPException(status_code=404, detail="Video not found")
    gorse_payload = dict(progress.payload or {})
    gorse_payload["percent_completed"] = state.get("percent_completed")
    gorse_event = "complete" if state.get("completed") else "watch_progress"
    gorse_state = _sync_gorse_event(user_key, youtube_video_id, gorse_event, gorse_payload)
    return {"ok": True, "progress": _json_ready_value(state), "gorse": _json_ready_value(gorse_state)}


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


def _download_filename(title: str, video_id: str, extension: str = "mp4") -> str:
    """A filename a file manager will accept, without leaking path separators."""
    cleaned = re.sub(r"[^\w\s.-]", "", title, flags=re.UNICODE).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)[:120].strip(" .")
    return f"{cleaned or video_id} [{video_id}].{extension}"


@app.get("/api/download/{youtube_video_id}")
def api_download_video(request: Request, youtube_video_id: str):
    """Stream the best progressive rendition back as a file download.

    Only muxed streams are offered: the high-resolution DASH renditions are separate
    audio and video tracks and would need remuxing before they are useful as a file.
    """
    auth_redirect = _login_required_redirect(request)
    if auth_redirect:
        return auth_redirect
    try:
        playback = playback_for_video(youtube_video_id)
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
    upstream = client.send(
        client.build_request("GET", stream_url, headers={"user-agent": "Mozilla/5.0"}),
        stream=True,
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
    items = []
    for raw in (payload.get("comments") or [])[: (COMMENT_REPLY_LIMIT if token else COMMENT_LIMIT)]:
        if not isinstance(raw, dict):
            continue
        replies = raw.get("replies") if isinstance(raw.get("replies"), dict) else {}
        items.append({
            "author": str(raw.get("author") or "Unknown"),
            "author_id": str(raw.get("authorId") or ""),
            "avatar_url": _comment_avatar(raw.get("authorThumbnails")),
            "content": str(raw.get("content") or ""),
            "like_count": int(raw.get("likeCount") or 0),
            "published_text": str(raw.get("publishedText") or ""),
            "is_owner": bool(raw.get("authorIsChannelOwner")),
            "is_pinned": bool(raw.get("isPinned")),
            "verified": bool(raw.get("verified")),
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
    }


@app.get("/api/sidebar/subscriptions", response_class=HTMLResponse)
def api_sidebar_subscriptions(request: Request) -> str:
    """The sidebar subscription list as HTML, so it can refresh in place after a
    subscribe or unsubscribe without a full page reload."""
    auth_redirect = _login_required_redirect(request)
    if auth_redirect:
        return ""
    return _sidebar_subscription_group()


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


def _notification_items(user_key: str, limit: int = 24) -> tuple[list[dict[str, Any]], int]:
    """Recent subscription uploads plus how many arrived since the panel was last opened."""
    preferences = db.get_feed_preferences(user_key)
    include_shorts = not preferences.get("exclude_shorts", False)
    rows = db.list_subscription_uploads(limit, user_key, include_shorts)
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
        slides = '<p class="empty-copy">No Shorts in your library yet. Sync FreshRSS, or turn Shorts on in Settings.</p>'
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


@app.post("/api/sync/freshrss")
def api_sync_freshrss() -> dict[str, Any]:
    result = sync_freshrss_once()
    if not result.get("ok") and result.get("status") == "configuration_error":
        raise HTTPException(status_code=409, detail=result)
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
              <description>{escape("Invidious discovery" if video.get("source_kind") == "discovery" else "FreshRSS subscription")}: {author}</description>
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
