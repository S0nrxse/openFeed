from __future__ import annotations

import base64
import difflib
import hashlib
import hmac
import json
import logging
import random
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
from functools import lru_cache
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
from . import netguard
from . import channel_tools
from . import search_extras
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
from . import discovery, downloads, opml, profiles, ryd, takeout, tokens, transcripts, ytdlp
from . import library_tools
from .playback import PlaybackError, download_options, playback_for_video
from .rss import fetch_channel_feed, parse_channel_feed, sync_rss_once
from .youtube_ids import CHANNEL_ID_STRICT_RE, youtube_channel_id_from_text
from . import dataexport, sharetime
from . import accounts, account_pages, signin
from . import oidc, passkeys, trusted_header
from . import outbound, privacy
from . import instance, policy
from . import catalog_scope
from . import admin_pages


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    # Accounts: map earlier Authentik users onto accounts, and print the first-run setup
    # code while no admin exists (account_pages.startup).
    account_pages.startup()
    # The v2 ranker's library index takes ~20 s to read the first time; built on a thread
    # so startup is not held up. Until it exists the classic ranker serves.
    try:
        from . import ranker

        ranker.warm()
    except Exception as exc:  # noqa: BLE001 - the classic ranker covers for it
        logger.info("ranker index warm-up not started: %s", exc)
    # Library search's description index takes about a minute to build the first time and
    # is built CONCURRENTLY, so reads and writes go on meanwhile. ILIKE serves until then.
    def _build_search_index() -> None:
        try:
            db.ensure_search_index()
        except Exception as exc:  # noqa: BLE001 - search falls back to ILIKE
            logger.info("search index not built: %s", exc)

    threading.Thread(target=_build_search_index, name="search-index", daemon=True).start()
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

## Profiles

One account can hold up to six household profiles, each with its own `user_key`. A
browser picks one at "Who's watching" and keeps it in a signed, HttpOnly cookie that is
honoured only for a profile of the signed-in account. A token acts as the profile it was
created under (the main profile's key is the account's own) and ignores that cookie.
`GET /api/` reports which profile a credential acts as. Adding, changing and deleting
profiles needs a browser session on a profile that is not a kids profile.

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
    # FastAPI's own /docs and /redoc pages load Swagger UI and ReDoc from a public CDN
    # (and ReDoc a Google font), which would send every reader's browser to a third party.
    # /docs is served by apidocs.py from this origin instead; /redoc redirects to it.
    docs_url=None,
    redoc_url=None,
)

logger = logging.getLogger("openfeed.web")
# No client addresses or query strings (search terms) in the access log unless
# OPENFEED_LOG_CLIENT_DETAILS is on. See privacy.AccessLogFilter.
privacy.install_log_filters()

TAG_RE = re.compile(r"<[^>]+>")


# API browser/docs surfaces; gated behind require_login by enforce_api_access. Without
# require_login these stay public: a local install relies on the loopback bind instead.
_DOC_PATHS = frozenset({"/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"})


def _utc_timestamp() -> int:
    return int(time.time())


def _safe_exc_name(exc: BaseException) -> str:
    """Exception text that is safe to show.

    httpx and psycopg messages embed internal endpoint URLs and connection
    details; handlers log the full exception and surface only the class name.
    """
    return type(exc).__name__ or "error"


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
    override = get_settings().cookie_secure.strip().lower()
    if override in {"1", "true", "yes", "on"}:
        return True
    if override in {"0", "false", "no", "off"}:
        return False
    return get_settings().public_url.startswith("https://")


def _safe_next_path(next_path: str | None, fallback: str = "/") -> str:
    parsed = urllib.parse.urlsplit(next_path or fallback)
    # Browsers read a backslash as a slash, so a next of /\evil.example would leave the site.
    if parsed.scheme or parsed.netloc or not parsed.path.startswith("/") or "\\" in parsed.path:
        return fallback
    return urllib.parse.urlunsplit(("", "", parsed.path, parsed.query, parsed.fragment))


def _safe_external_href(url: str | None) -> str:
    """Sanitize an externally-supplied link, returning it raw when browsable.

    Feed-supplied entry links and channel URLs can carry any scheme; javascript: or
    data: hrefs execute in the app origin when clicked, so anything that is not a
    plain http(s) URL drops out. Callers still escape() the result.
    """
    candidate = str(url or "").strip()
    if candidate.lower().startswith(("http://", "https://")):
        return candidate
    return ""


def _request_host(request: Request) -> str:
    forwarded_host = str(request.headers.get("x-forwarded-host") or "").split(",", 1)[0].strip()
    return (forwarded_host or str(request.headers.get("host") or "")).lower()


def _canonical_login_redirect(request: Request, next_path: str | None) -> RedirectResponse | None:
    """Send sign-in to the configured public address (cookies and OIDC callbacks belong
    there). Never to the built-in default: an install that did not set OPENFEED_PUBLIC_URL
    signs in wherever it was opened. getattr: stand-in Settings in tests lack the field."""
    settings = get_settings()
    if not settings.public_url or not getattr(settings, "public_url_configured", True):
        return None
    public = urllib.parse.urlsplit(settings.public_url)
    public_host = str(public.netloc or "").lower()
    if not public.scheme or not public_host:
        return None
    if _request_host(request) == public_host:
        return None
    target = f"{settings.public_url}/auth/login?next={urllib.parse.quote(_safe_next_path(next_path))}"
    return RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)


def _session_from_request(request: Request) -> dict[str, Any] | None:
    session = _unsign_payload(request.cookies.get(get_settings().session_cookie))
    if not session or int(session.get("expires_at", 0)) <= _utc_timestamp():
        return None
    if not str(session.get("sub") or "").strip():
        return None
    return session


def _tokens_without_sign_in() -> bool:
    """Sign-in is off and nothing requires it: the install's one local account (and its
    profiles) may hold API tokens. The API answers without one too, but a token still says
    which profile a script acts as, and lets a client that insists on a key connect."""
    settings = get_settings()
    return settings.auth_mode != "authentik_oidc" and not settings.require_login


def _local_token_owner(user_key: str) -> bool:
    """A token minted with sign-in off: owned by the local account or one of its profiles."""
    return user_key == "local" or user_key.startswith("local:p:")


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
    # Minted while the server was open to anyone. Once sign-in is on (or required) those
    # must not outlive the change and let an unknown caller past it.
    if _local_token_owner(str(record.get("user_key") or "local")) and not _tokens_without_sign_in():
        return None
    # With sign-in on, a token works only while its account does: disabled stops it at once
    # (deleting an account deletes its tokens). Authentik users from before accounts may
    # have no account row yet; built-in accounts mode accepts only an active account.
    if get_settings().sign_in_enabled and not _local_token_owner(str(record.get("user_key") or "local")):
        try:
            state = accounts.owner_state(str(record.get("user_key") or ""))
        except Exception:
            return None
        if state == "disabled" or (state == "unknown" and get_settings().auth_mode == "accounts"):
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
    # Server-side sessions (signin.py). The stateless signed cookie from before accounts is
    # honoured only with sign-in off, as it always was; with sign-in on, harden_http trades
    # an Authentik one for a server session once (signin.exchange_legacy_cookie).
    session_user = signin.user_from_request(request) if request is not None else None
    if session_user:
        return _with_profile(request, session_user)
    session = _session_from_request(request) if request is not None and not get_settings().sign_in_enabled else None
    if session:
        email = str(session.get("email") or "").strip().lower()
        name = str(session.get("name") or session.get("username") or email or "OpenFeed user").strip()
        sub = str(session.get("sub") or "").strip()
        return _with_profile(request, {"authenticated": True, "user_key": sub, "sub": sub, "email": email, "display_name": name, "username": str(session.get("username") or ""), "groups": session.get("groups") if isinstance(session.get("groups"), list) else [], "iat": int(session["iat"]) if str(session.get("iat") or "").isdigit() else 0})
    return _with_profile(request, {"authenticated": False, "user_key": "local", "sub": "local", "email": "", "display_name": "Local OpenFeed", "username": "local", "groups": []})


PROFILE_COOKIE = "openfeed_profile"
PROFILE_COOKIE_SECONDS = 400 * 24 * 3600
_PROFILE_SCOPE_KEY = "openfeed.profiles"


def _account_profiles(request: Request | None, account_key: str) -> list[dict[str, Any]]:
    """The account's stored profiles, read once per request.

    _current_user runs several times per request (middleware, shell, handler), so the
    list is kept on the ASGI scope, which the middleware and the route share. A database
    failure reads as "no profiles": the request then acts as the main profile, which is
    the account's own key and never another account's.
    """
    scope = getattr(request, "scope", None)
    cache = scope.setdefault(_PROFILE_SCOPE_KEY, {}) if isinstance(scope, dict) else None
    if cache is not None and account_key in cache:
        return cache[account_key]
    try:
        rows = profiles.list_profiles(account_key)
    except Exception:  # noqa: BLE001 - fail closed to the account's own key
        rows = []
    if cache is not None:
        cache[account_key] = rows
    return rows


def _forget_account_profiles(request: Request | None) -> None:
    scope = getattr(request, "scope", None)
    if isinstance(scope, dict):
        scope.pop(_PROFILE_SCOPE_KEY, None)


def _profile_cookie_value(account_key: str, profile_id: str) -> str:
    """Signed and bound to the account when a session secret exists, else the bare id.

    Either way the id is only honoured if it is listed under the requesting account, so
    the signature is defence in depth rather than the check itself.
    """
    if _auth_secret():
        return _sign_payload({"a": account_key, "p": profile_id})
    return profile_id


def _profile_cookie_choice(request: Request | None, account_key: str) -> str:
    cookies = getattr(request, "cookies", None) or {}
    raw = str(cookies.get(PROFILE_COOKIE) or "")
    if not raw:
        return ""
    if _auth_secret():
        payload = _unsign_payload(raw)
        if not payload or payload.get("a") != account_key:
            return ""
        raw = str(payload.get("p") or "")
    return raw if _valid_uuid(raw) else ""


def _with_profile(request: Request | None, user: dict[str, Any]) -> dict[str, Any]:
    """Point user_key at the chosen profile of this account.

    account_key keeps the account's own identity. The cookie only nominates a profile id;
    the user_key always comes from the account's own rows, so a forged or replayed cookie
    naming another account's profile matches nothing and the request stays on the main
    profile.
    """
    account_key = str(user.get("user_key") or "local")
    user["account_key"] = account_key
    if request is None:
        user["profiles"], user["profile"], user["profile_chosen"] = [], None, False
        return user
    rows = _account_profiles(request, account_key)
    choice = _profile_cookie_choice(request, account_key) if rows else ""
    active = next((row for row in rows if row.get("id") == choice and row.get("account_key") == account_key), None) if choice else None
    main_row = next((row for row in rows if row.get("is_main")), None)
    user["profiles"] = rows
    user["profile_chosen"] = active is not None
    user["profile"] = active or main_row
    if active:
        user["user_key"] = str(active["user_key"])
    return user


def _profile_main_name(user: dict[str, Any]) -> str:
    return str(user.get("display_name") or "").strip() if user.get("authenticated") else "Main"


def _profile_list(user: dict[str, Any]) -> list[dict[str, Any]]:
    """Stored profiles, or the implicit main profile when none are stored yet."""
    rows = list(user.get("profiles") or [])
    return rows or [profiles.implicit_main(str(user.get("account_key") or user.get("user_key") or "local"), _profile_main_name(user))]


def _active_profile(user: dict[str, Any]) -> dict[str, Any]:
    return user.get("profile") or _profile_list(user)[0]


def _needs_profile_choice(user: dict[str, Any]) -> bool:
    return not user.get("via_token") and len(user.get("profiles") or []) > 1 and not user.get("profile_chosen")


def _auth_groups(user: dict[str, Any]) -> set[str]:
    groups = user.get("groups") if isinstance(user.get("groups"), list) else []
    return {str(group) for group in groups if str(group).strip()}


def _user_has_required_access(user: dict[str, Any]) -> bool:
    # A verified token delegates its authorized issuer's app access, not IdP groups.
    # This also keeps tokens issued before delegation metadata was added usable.
    if user.get("authenticated") and user.get("via_token"):
        return True
    # The access group is an identity-provider claim. An account that signed in with its
    # own password was created by an admin (or an invite), which is the access decision.
    if user.get("session_backed") and not user.get("idp"):
        return True
    settings = get_settings()
    # Each OIDC provider (and the trusted header) has its own access and admin groups;
    # sessions without one, and the original single provider, use the global settings.
    admin_groups, required_group = signin.idp_policy(user)
    if not required_group:
        return True
    if not user.get("authenticated"):
        return not settings.require_login
    groups = _auth_groups(user)
    return required_group in groups or bool(groups & admin_groups)


def _denied_by_idp_group(user: dict[str, Any], admin_only: bool) -> bool:
    """Whether a refusal comes from identity-provider groups, the only case the 403 page
    talks about an IdP. Missing the access group always does. For admin pages: a sign-in
    through a provider whose admin groups are configured, or the provider's old signed cookie.
    Built-in accounts, and providers without admin groups, are admins by role."""
    if not admin_only:
        return True
    if user.get("session_backed"):
        admin_groups, _required = signin.idp_policy(user)
        return bool(user.get("idp") and admin_groups)
    # Signed in without a server session: the identity provider's signed cookie.
    return bool(user.get("authenticated"))


def _access_denied_response(user: dict[str, Any] | None = None, admin_only: bool = False) -> Response:
    user = user or {}
    if not _denied_by_idp_group(user, admin_only):
        return _auth_error_page(
            "Admins only",
            "This page is for admins of this OpenFeed.",
            fix="If you need it, ask an admin of this OpenFeed.",
            account=str(user.get("username") or user.get("display_name") or "") if user.get("authenticated") else "",
            actions=[("Return to OpenFeed", "/", True), ("Account & security", "/account", False)]
            if user.get("session_backed") else [("Return to OpenFeed", "/", True)],
            status_code=status.HTTP_403_FORBIDDEN,
        )
    required_group = get_settings().required_group or "the configured OpenFeed access group"
    return _auth_error_page(
        "OpenFeed access denied",
        "This page is for administrators, and your account is not in an admin group." if admin_only else f"Your account is missing {required_group}.",
        fix="Ask your identity-provider administrator to grant the matching group, then try again.",
        account=str(user.get("email") or user.get("display_name") or "") if user.get("authenticated") else "",
        actions=[("Try again", "", True), ("Sign out", "/auth/logout", False)] if user.get("authenticated") else [("Try again", "/auth/login", True)],
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
        if not (user.get("authenticated") and user.get("token_delegated_admin") is True):
            return False
        return _token_owner_still_admin(str(user.get("user_key") or ""))
    # Server sessions: the role stored on the account, or an IdP admin group (signin.is_admin).
    if user.get("session_backed"):
        return signin.is_admin(user)
    settings = get_settings()
    if settings.auth_mode == "accounts":
        return False
    if not settings.require_login or not settings.admin_groups:
        return True
    # IdP groups are snapshotted into the signed cookie at login and the IdP is not
    # consulted on every request. Cap how long a session can keep exercising admin
    # rights without a fresh login so a demoted account (or a stolen cookie) loses
    # admin staying power well before the session TTL expires. 0 restores the old
    # "valid as long as the session" behaviour.
    max_admin_age = settings.admin_session_seconds
    if max_admin_age > 0:
        issued_at = user.get("iat")
        if not isinstance(issued_at, int) or issued_at <= 0 or _utc_timestamp() - issued_at > max_admin_age:
            return False
    return bool(_auth_groups(user) & settings.admin_groups)


def _token_owner_still_admin(user_key: str) -> bool:
    """A token minted by an admin keeps admin rights only while its account is an admin.

    The capability is snapshotted at mint time; without this check an admin demoted to
    member kept every admin route through the tokens they made before. With built-in
    accounts the role in the database decides. Elsewhere (sign-in off, IdP groups) the
    snapshot stands, as it did before roles were stored.
    """
    if get_settings().auth_mode != "accounts":
        return True
    try:
        account = accounts.get_by_user_key(user_key.split(":p:", 1)[0])
    except Exception:  # noqa: BLE001 - unknown reads as not admin
        return False
    return bool(account and account.get("role") == "admin" and not account.get("disabled"))


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
        if get_settings().require_login and path in _DOC_PATHS and not _current_user(request).get("authenticated"):
            return JSONResponse({"detail": "login_required"}, status_code=status.HTTP_401_UNAUTHORIZED)
        # Per-account rate limits on expensive pages (search, channels, watch). policy.py.
        if policy.shared_install() and policy.limit_for(request.method, path):
            limited = policy.refusal(request, _current_user(request))
            if limited is not None:
                return limited
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
        return _login_required_redirect(request) or _access_denied_response(user)
    if user.get("authenticated") and not _user_has_required_access(user):
        if wants_json:
            return JSONResponse({"detail": "openfeed_access_denied"}, status_code=status.HTTP_403_FORBIDDEN)
        return _access_denied_response(user)

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
    if path.startswith("/api/tokens") and not user.get("authenticated") and not _tokens_without_sign_in():
        return JSONResponse({"detail": "session_required"}, status_code=status.HTTP_401_UNAUTHORIZED)
    if path.startswith("/admin/") and not _user_is_admin(user):
        return _access_denied_response(user, admin_only=True)
    # Admin-only API routes and per-account rate limits on a shared install (policy.py).
    refused = policy.refusal(request, user)
    if refused is not None:
        return refused
    # /api/status/panel serves the same payload as /api/status/full; it has to sit behind
    # the same gate or every signed-in user can read internal state that only admins should.
    if path in {"/api/status/full", "/api/status/panel", "/api/sync/runs"} and not _user_is_admin(user):
        return JSONResponse({"detail": "admin_required"}, status_code=status.HTTP_403_FORBIDDEN)

    response = await call_next(request)
    if path.startswith("/api/tokens"):
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["Pragma"] = "no-cache"
    return response


# The base page policy (privacy.content_security_policy). A response that frames a
# third-party player the viewer opted into adds exactly that origin, per response, through
# request.state.frame_origins; nothing else ever widens it.
_PAGE_CSP = privacy.content_security_policy()
_FRAME_BLOCK_NOTE = "Turned off on this server. The admin has not allowed players from other sites."


def _other_site_players_blocked() -> bool:
    """True while the admin allows neither the YouTube player nor public instances.

    Settings and the failure panel read this so that their advice follows what is possible.
    """
    return not (privacy.youtube_allowed() or privacy.public_instances_allowed())


def _fallback_remedy(when_allowed: str) -> str:
    """Advice that leans on the YouTube player, or why that player cannot help here."""
    if not privacy.youtube_allowed():
        return "The YouTube player cannot help: it is turned off on this server."
    return when_allowed


def _upgrade_legacy_session(request: Request) -> None:
    """Swap a pre-accounts signed Authentik cookie for a server session, once.

    Only in authentik_oidc mode, where such cookies exist. The new cookie is written into
    this request's headers, so every later step already sees the server session, and onto
    the response by harden_http.
    """
    settings = get_settings()
    if settings.auth_mode != "authentik_oidc":
        return
    raw = str(request.cookies.get(settings.session_cookie) or "")
    if not raw or raw.startswith(signin.SESSION_PREFIX):
        return
    payload = _unsign_payload(raw)
    token = None
    if payload:
        try:
            token = signin.exchange_legacy_cookie(request, payload, signin.OIDC.key)
        except Exception:  # noqa: BLE001 - fall back to signed out
            logger.warning("could not upgrade a signed-in session", exc_info=True)
    if not token:
        return
    cookie = f"{settings.session_cookie}={token}"
    headers = [(k, v) for k, v in request.scope.get("headers", []) if k != b"cookie"]
    others = "; ".join(f"{k}={v}" for k, v in request.cookies.items() if k != settings.session_cookie)
    headers.append((b"cookie", (cookie + ("; " + others if others else "")).encode("latin-1")))
    request.scope["headers"] = headers
    request.scope["openfeed.upgraded_session"] = token
    for attr in ("_cookies", "_headers"):
        request.__dict__.pop(attr, None)


@app.middleware("http")
async def harden_http(request: Request, call_next):
    """Browser-facing hardening applied outside the access middleware so error responses get it too.

    - Origin check on writes: browsers always attach Origin to cross-site POST/PATCH/
      DELETE, so a mismatched one is CSRF even where SameSite=Lax is not enforced.
      Requests without Origin (curl, server-to-server) are left alone.
    - Standard security headers, including a CSP that keeps external scripts out of the
      origin even though inline scripts are used by the app shell.
    """
    if request.method not in {"GET", "HEAD", "OPTIONS"} and request.headers.get("origin"):
        parsed = urllib.parse.urlsplit(request.headers["origin"].strip())
        if parsed.netloc:
            allowed = {_request_host(request), urllib.parse.urlsplit(get_settings().public_url).netloc}
            if parsed.netloc.lower() not in {h.lower() for h in allowed if h}:
                return JSONResponse({"detail": "cross_origin_blocked"}, status_code=status.HTTP_403_FORBIDDEN)
    # A write that carries a server session cookie, or targets a sign-in form, must show it
    # came from this site: an Origin, or a Referer when a privacy setting dropped Origin.
    if signin.csrf_failure(request):
        return JSONResponse({"detail": "cross_origin_blocked"}, status_code=status.HTTP_403_FORBIDDEN)
    _upgrade_legacy_session(request)
    # A forward-auth proxy's user header: believed only from OPENFEED_TRUSTED_PROXIES,
    # stripped from anyone else's request (trusted_header.py).
    trusted_header.apply(request)
    response = await call_next(request)
    new_token = request.scope.pop("openfeed.upgraded_session", None)
    if new_token:
        signin.set_session_cookie(response, new_token)
    # CSP, Referrer-Policy, Permissions-Policy and friends, in privacy.apply_headers. Only the
    # proxied Invidious embed gets video.js's data: font and blob: worker. A route that frames
    # a third-party player the viewer opted into names that origin on request.state.
    privacy.apply_headers(response.headers, request.url.path, getattr(request.state, "frame_origins", ()) or ())
    return response


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
        return _access_denied_response(user)
    # "Who's watching": pages wait for a choice; the API acts as the main profile instead,
    # because a script has nobody to ask.
    path = str(getattr(getattr(request, "url", None), "path", "") or "")
    if _needs_profile_choice(user) and not path.startswith("/api/"):
        next_path = path + (("?" + request.url.query) if request.url.query else "")
        return RedirectResponse(f"/profiles?next={urllib.parse.quote(_safe_next_path(next_path))}", status_code=status.HTTP_303_SEE_OTHER)
    return None


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


# Inlined, not linked: these pages answer when the session, the OIDC provider or the shell
# itself is the thing that is broken, so they must render from this one response. The
# values mirror assets/css/00-tokens.css and the .btn family; a test keeps them equal.
_AUTH_PAGE_CSS = """
:root { color-scheme: dark; font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Inter", "Segoe UI", Roboto, system-ui, sans-serif;
  --bg: #0a0a0b; --surface: #121214; --surface-2: #1a1a1d; --surface-3: #232327; --border: rgba(255,255,255,.09); --border-soft: rgba(255,255,255,.055);
  --text: #ededef; --muted: #9a9aa3; --muted-2: #84848d; --bone: #ededef; --bone-hover: #fff; --on-bone: #0a0a0b; --accent: #e5484d;
  --r-panel: 14px; --r-control: 8px; --focus: 0 0 0 2px rgba(255,255,255,.5); }
:root[data-theme="light"] { color-scheme: light; LIGHT_TOKENS }
@media (prefers-color-scheme: light) { :root[data-theme="system"] { color-scheme: light; LIGHT_TOKENS } }
*, *::before, *::after { box-sizing: border-box; }
body { margin: 0; min-height: 100vh; min-height: 100dvh; display: grid; place-items: center; padding: 16px; background: var(--bg); color: var(--text); font-size: 13.5px; line-height: 1.45; }
main { width: min(520px, 100%); display: grid; gap: 14px; padding: 24px; border: 1px solid var(--border); border-radius: var(--r-panel); background: var(--surface); }
main > * { margin: 0; min-width: 0; }
[hidden] { display: none !important; }
.eyebrow { display: flex; align-items: center; gap: 8px; color: var(--muted-2); font-size: 11px; font-weight: 600; line-height: 1.2; letter-spacing: .07em; text-transform: uppercase; }
.dot { width: 8px; height: 8px; flex: 0 0 auto; border-radius: 999px; background: var(--accent); box-shadow: 0 0 8px rgba(229,72,77,.4); }
h1 { font-size: 22px; font-weight: 650; line-height: 1.25; letter-spacing: -.02em; overflow-wrap: anywhere; }
p { color: var(--muted); overflow-wrap: anywhere; }
.block { display: grid; gap: 6px; padding: 12px; border: 1px solid var(--border-soft); border-radius: var(--r-control); background: var(--bg); }
.block-label { color: var(--muted-2); font-size: 11px; font-weight: 600; line-height: 1.2; letter-spacing: .07em; text-transform: uppercase; }
.env-row { display: flex; align-items: center; justify-content: space-between; gap: 10px; min-height: 32px; }
code { min-width: 0; color: var(--text); font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; overflow-wrap: anywhere; }
.actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 2px; }
.btn { display: inline-flex; align-items: center; justify-content: center; min-height: 36px; padding: 0 15px; border: 1px solid transparent; border-radius: 999px; background: var(--surface-2); color: var(--text); font: inherit; font-size: 13px; font-weight: 550; letter-spacing: -.005em; text-decoration: none; cursor: pointer; transition: background 140ms ease, transform 140ms ease; }
.btn:hover { background: var(--surface-3); border-color: var(--border-soft); }
.btn:active { transform: scale(.96); }
.btn:focus-visible { outline: none; box-shadow: var(--focus); }
.btn-primary { background: var(--bone); color: var(--on-bone); font-weight: 600; }
.btn-primary:hover { background: var(--bone-hover); border-color: transparent; }
.btn-sm { min-height: 32px; padding: 0 14px; flex: 0 0 auto; }
@media (pointer: coarse) { .btn { min-height: 44px; } .btn-sm { min-height: 44px; } }
@media (prefers-reduced-motion: reduce) { .btn { transition: none; } .btn:active { transform: none; } }
""".replace("LIGHT_TOKENS", (
    # The light set from 00-tokens.css, for the same tokens; test_theme keeps them equal.
    "--bg: #ffffff; --surface: #f8f8f9; --surface-2: #f1f1f3; --surface-3: #e6e6e9; --border: rgba(0,0,0,.12); "
    "--border-soft: rgba(0,0,0,.08); --text: #0f0f10; --muted: #5c5c63; --muted-2: #686870; --bone: #0f0f10; "
    "--bone-hover: #000; --on-bone: #ffffff; --accent: #e5484d; --focus: 0 0 0 2px rgba(0,0,0,.6);"
))


def _auth_page_theme_head() -> str:
    """The sign-in error page knows no user, so the head script reads this browser's mirror."""
    from . import appearance

    return appearance.head()

# Progressive: with no script the buttons stay hidden and the names are still selectable.
_AUTH_PAGE_SCRIPT = """
document.querySelectorAll('[data-copy]').forEach((button) => {
  button.hidden = false;
  button.addEventListener('click', async () => {
    const label = button.textContent;
    try {
      await navigator.clipboard.writeText(button.dataset.copy);
      button.textContent = 'Copied';
    } catch (error) {
      const range = document.createRange();
      range.selectNodeContents(button.previousElementSibling);
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
      button.textContent = 'Press copy';
    }
    window.setTimeout(() => { button.textContent = label; }, 1800);
  });
});
"""


# The OIDC helpers raise short codes. On a page, the person needs the sentence.
_AUTH_DETAIL_TEXT = {
    "oidc_issuer_unconfigured": "OPENFEED_OIDC_ISSUER is not set, so OpenFeed does not know where to send you.",
    "oidc_discovery_unavailable": "OpenFeed could not reach the identity provider's discovery document. Check that Authentik is running and that OPENFEED_OIDC_ISSUER is correct.",
    "oidc_discovery_invalid": "The identity provider answered, but its discovery document could not be read. Check OPENFEED_OIDC_ISSUER.",
    "oidc_endpoints_unavailable": "The identity provider's discovery document does not list the endpoints OpenFeed needs.",
    "oidc_token_missing": "The identity provider did not return an access token.",
}


def _auth_error_page(
    title: str,
    detail: str,
    missing: list[str] | None = None,
    status_code: int = 503,
    *,
    fix: str = "",
    account: str = "",
    actions: list[tuple[str, str, bool]] | None = None,
) -> Response:
    """Cause first, then the missing settings, then the fix, then actions.

    actions: (label, href, primary). An empty href means reload this page.
    """
    missing_rows = "".join(
        f'<div class="env-row"><code>{escape(item)}</code>'
        # "A or B" lists alternatives; the name you would paste is the first.
        f'<button class="btn btn-sm" type="button" data-copy="{escape(item.split(" or ")[0], quote=True)}" '
        f'aria-label="Copy {escape(item.split(" or ")[0], quote=True)}" hidden>Copy</button></div>'
        for item in (missing or [])
    )
    missing_html = (
        f'<div class="block"><span class="block-label">Missing settings</span>{missing_rows}</div>' if missing_rows else ""
    )
    account_html = (
        f'<div class="block"><span class="block-label">Signed in as</span><code>{escape(account)}</code></div>' if account else ""
    )
    if missing and not fix:
        fix = "Add them to the .env file next to compose.yml, then run ./deploy.sh."
    fix_html = f"<p>{escape(fix)}</p>" if fix else ""
    if actions is None:
        actions = [("Return to OpenFeed", "/", True)] if status_code == 404 else [("Try again", "/auth/login", True), ("Return to OpenFeed", "/", False)]
    actions_html = "".join(
        f'<a class="btn{" btn-primary" if primary else ""}" href="{escape(href or "", quote=True)}">{escape(label)}</a>'
        for label, href, primary in actions
    )
    reason = "Access" if status_code == 403 else "Sign-in"
    content = (
        f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'
        f'{_auth_page_theme_head()}<title>{escape(title)} - OpenFeed</title>{_head_icons()}'
        f"<style>{_AUTH_PAGE_CSS}</style></head><body><main>"
        f'<p class="eyebrow"><span class="dot" aria-hidden="true"></span>{reason} {int(status_code)}</p>'
        f"<h1>{escape(title)}</h1><p>{escape(_AUTH_DETAIL_TEXT.get(detail, detail))}</p>{account_html}{missing_html}{fix_html}"
        f'<div class="actions">{actions_html}</div></main><script>{_AUTH_PAGE_SCRIPT}</script></body></html>'
    )
    return Response(content, status_code=status_code, media_type="text/html; charset=utf-8")


def _set_auth_session_cookie(response: Response, payload: dict[str, Any]) -> None:
    response.set_cookie(get_settings().session_cookie, _sign_payload(payload), max_age=get_settings().auth_session_ttl_seconds, httponly=True, secure=_cookie_secure(), samesite="lax", path="/")


def _clear_auth_session_cookie(response: Response) -> None:
    response.delete_cookie(get_settings().session_cookie, path="/", secure=_cookie_secure(), samesite="lax")


def _clear_auth_state_cookie(response: Response) -> None:
    response.delete_cookie(get_settings().oidc_state_cookie, path="/", secure=_cookie_secure(), samesite="lax")


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
        # Only ever a same-origin URL: anything else would send the browser elsewhere.
        return privacy.same_origin_image(thumb)
    if not thumb or "/vi/" in thumb:
        return f"/vi/{video_id}/hqdefault.jpg"
    return privacy.same_origin_image(thumb) or f"/vi/{video_id}/hqdefault.jpg"


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
    "mark_unwatched",
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
    captions_enabled: bool | None = None
    captions_language: str | None = None
    captions_auto: bool | None = None
    live_chat_enabled: bool | None = None
    autoplay: bool | None = None
    default_speed: float | None = None


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
    # private, unlisted or public (library_tools.VISIBILITIES). None leaves it as it is.
    visibility: str | None = None


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
    dearrow_enabled: bool | None = None
    dearrow_casing: str | None = None
    dearrow_thumbnails: bool | None = None
    show_posts_in_feeds: bool | None = None
    muted_channels: list[str] | None = None
    setup_dismissed: bool | None = None
    # "", "graph", "blend" or "classic"; "" follows ranker.DEFAULT_MODE.
    ranker_mode: str | None = None
    explore_enabled: bool | None = None


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
    # Only what the response depends on. The viewer's user agent, cookies and address
    # stay here: Invidious answers the same without them.
    allowed = {"accept", "accept-language", "range", "if-range"}
    return {key: value for key, value in request.headers.items() if key.lower() in allowed}


def _response_headers(headers: httpx.Headers) -> dict[str, str]:
    blocked = HOP_BY_HOP_HEADERS | {"content-encoding", "content-length"}
    return {key: value for key, value in headers.items() if key.lower() not in blocked}


def proxy_invidious(request: Request, upstream_path: str, answer_missing_now: bool = False, require_media: bool = False):
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
        raise HTTPException(status_code=502, detail=f"Invidious request failed: {_safe_exc_name(exc)}") from exc
    if answer_missing_now and upstream_response.status_code == 404:
        # A missing thumbnail arrives as 404 headers over a body Invidious never ends, so
        # relaying it would hold the image loading until the read timeout, and the card
        # on its shimmer, with the fallback waiting behind it.
        upstream_response.close()
        return Response(status_code=404)
    headers = _response_headers(upstream_response.headers)
    if require_media and request.method == "GET" and upstream_response.status_code == 200:
        # A live stream can arrive as a valid MPD with no AdaptationSet in it. Video.js has
        # no answer for that shape: its parser throws an uncaught TypeError and the player
        # sits there. A failed request is one it reports, so the source ladder moves on.
        body = upstream_response.read()
        upstream_response.close()
        if b"<AdaptationSet" not in body:
            return Response(status_code=502)
        return Response(body, headers=headers)
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
    "api-tokens-v1.js": "text/javascript",
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


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _shell_asset(digest: str, body: str, media_type: str) -> Response:
    """Serve the shell bundle under its own hash.

    The URL names the bytes, so it can be cached for ever: a new build gets a new URL from
    the page that links it. A stale hash is a 404 rather than the current bytes, because
    answering it would pin different content to that URL in every shared cache. Nothing
    here reads the request or the session, and these paths sit outside the /api and /admin
    gates, so login and error paths load them too.
    """
    if digest != _content_hash(body):
        raise HTTPException(status_code=404, detail="Not found")
    return Response(body, media_type=media_type, headers={"cache-control": "public, max-age=31536000, immutable"})


@app.get("/assets/shell/{digest}.css", include_in_schema=False)
@app.head("/assets/shell/{digest}.css", include_in_schema=False)
def shell_stylesheet(digest: str) -> Response:
    return _shell_asset(digest, _home_styles(), "text/css")


@app.get("/assets/shell/{digest}.js", include_in_schema=False)
@app.head("/assets/shell/{digest}.js", include_in_schema=False)
def shell_script(digest: str) -> Response:
    return _shell_asset(digest, _home_script(), "text/javascript")


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


def _status_section(title: str, body: str, subtitle: str = "", actions: str = "") -> str:
    """title and subtitle are escaped. body and actions are trusted HTML."""
    sub = f"<p>{escape(subtitle)}</p>" if subtitle else ""
    action_html = f'<div class="status-head-actions">{actions}</div>' if actions else ""
    return (
        f'<section class="status-block"><div class="status-head"><div><h3>{escape(title)}</h3>{sub}</div>{action_html}</div>'
        f"{body}</section>"
    )


def _status_fold(title: str, count: int, body: str) -> str:
    return (
        f'<details class="status-fold"><summary>{escape(title)} <span class="page-meta">{int(count)}</span></summary>'
        f"{body}</details>"
    )


def _raw_output(label: str, text: str) -> str:
    """Raw text behind a disclosure, with a Copy that the shared handler reads from <pre>."""
    return (
        f'<details class="raw-output"><summary>{escape(label)}</summary>'
        f'<div class="raw-output-body"><button class="btn btn-sm" type="button" data-copy-raw>Copy</button>'
        f"<pre>{escape(text)}</pre></div></details>"
    )


_STATUS_CHECK_LABELS = {
    "database": "Postgres", "redis": "Redis", "rss_poller": "RSS poller",
    "invidious_api": "Invidious API", "invidious_player": "Invidious player",
    "recommended_feed": "Recommended feed",
}


def _status_check_cause(item: dict[str, Any]) -> str:
    """The most specific reason a check gave, or empty if it gave none."""
    for field in ("likely_cause", "error", "detail"):
        if item.get(field):
            return _text_excerpt(str(item[field]), 120)
    return f"HTTP {item['status_code']}" if item.get("status_code") else ""


def _status_source_rows(user_key: str) -> list[dict[str, Any]]:
    """Every source in ladder order, with its live health. Read failures show as no sources."""
    rows: list[dict[str, Any]] = []
    try:
        degraded = playback_backend_degraded()
        for source in _all_playback_sources(user_key):
            health = source_health_snapshot(source["id"])
            local_skipped = degraded and source["kind"] == "invidious-local"
            rows.append({
                **source,
                "health": health,
                "cooling": source_in_cooldown(source["id"]) or local_skipped,
                "failing": not source_in_cooldown(source["id"]) and bool(health.get("failures")),
            })
    except Exception as exc:
        logger.warning("status playback read failed: %s", exc)
    return rows


def _status_verdict(status: dict[str, Any], sources: list[dict[str, Any]]) -> tuple[str, str, str]:
    """(tone, headline, detail), from measured state only. tone is ok, warn or bad."""
    checks = status.get("checks") or {}
    failing = [key for key in _STATUS_CHECK_LABELS if not (checks.get(key) or {}).get("ok")]
    passing_text = f"{len(_STATUS_CHECK_LABELS) - len(failing)} of {len(_STATUS_CHECK_LABELS)} checks pass."

    def named(key: str) -> str:
        cause = _status_check_cause(checks.get(key) or {})
        label = _STATUS_CHECK_LABELS[key]
        # Some checks already name themselves, and "X is failing: X has not run" reads badly.
        if cause.lower().startswith(label.lower()):
            return cause
        return f"{label} is failing: {cause}" if cause else f"{label} is failing"

    for key in ("database", "redis"):
        if key in failing:
            return "bad", named(key), passing_text
    enabled = [source for source in sources if source.get("enabled", True)]
    usable = [source for source in enabled if not source["cooling"]]
    if not sources or not enabled:
        return "bad", "Playback is off: no source is enabled", passing_text
    if not usable:
        return "bad", "Playback is down: every enabled source is paused", passing_text
    active = usable[0]
    label = str(active["label"])
    if active is not enabled[0]:
        return "warn", f"Playback degraded: {enabled[0]['label']} is paused, using {label}", passing_text
    if failing:
        return "warn", named(failing[0]), f"{passing_text} Playback still starts with {label}."
    if (active.get("health") or {}).get("status") == "healthy":
        return "ok", f"Playback healthy via {label}", passing_text
    return "ok", f"All checks pass. {label} is first in line", "It has not played anything since this server started."


def _status_ladder(sources: list[dict[str, Any]]) -> str:
    """The sources as a stepper. The one a play would use now is inverted."""
    if not sources:
        return '<p class="dialog-note">No playback sources are configured.</p>'
    enabled = [source for source in sources if source.get("enabled", True)]
    active_id = next((source["id"] for source in enabled if not source["cooling"]), "")
    items = []
    for position, source in enumerate(sources, start=1):
        health = source.get("health") or {}
        failures = int(health.get("failures") or 0)
        if not source.get("enabled", True):
            tone, state, extra = "off", "Off", ""
        elif source["cooling"]:
            tone, state = "hold", "Cooling down"
            seconds = int(health.get("cooldown_seconds") or 0)
            extra = f'<span data-cooldown="{seconds}">retrying in {seconds}s</span>' if seconds else "<span>skipped for now</span>"
        elif source["failing"]:
            tone, state, extra = "bad", "Failing", ""
        elif health.get("status") == "healthy":
            age = health.get("last_ok")
            tone, state, extra = "ok", "Healthy", f"<span>worked {int(age or 0)}s ago</span>"
        else:
            tone, state, extra = "idle", "Not tried yet", ""
        failure_text = f'<span>{failures} recent failure{"" if failures == 1 else "s"}</span>' if failures else ""
        chip = '<span class="ladder-chip">Active</span>' if source["id"] == active_id else ""
        current = ' aria-current="step"' if source["id"] == active_id else ""
        items.append(
            f'<li class="ladder-step{" is-off" if tone == "off" else ""}"{current}>'
            f'<span class="ladder-node">{position}</span>'
            f'<div class="ladder-copy"><strong>{escape(str(source["label"]))}</strong>{chip}'
            f'<small class="ladder-state"><i class="status-dot is-{tone}" aria-hidden="true"></i><span>{state}</span>{extra}{failure_text}</small></div></li>'
        )
    return f'<ol class="ladder">{"".join(items)}</ol>'


def _status_checks_table(checks: dict[str, Any]) -> str:
    rows = []
    for key, label in _STATUS_CHECK_LABELS.items():
        item = checks.get(key) or {}
        ok = bool(item.get("ok"))
        latency = item.get("latency_ms")
        facts = [f"HTTP {item['status_code']}"] if item.get("status_code") is not None else []
        facts += [f"{item['items']} items"] if item.get("items") is not None else []
        facts += [str(item[field]) for field in ("likely_cause", "error", "detail") if item.get(field)]
        detail = " &middot; ".join(escape(fact) for fact in facts)
        rows.append(
            f'<tr{"" if ok else " class=is-broken"}><td><strong>{escape(label)}</strong>'
            f'{f"<small>{detail}</small>" if detail else ""}</td>'
            f'<td><span class="hop-badge is-{"ok" if ok else "bad"}">{"OK" if ok else "Failed"}</span></td>'
            f'<td class="hop-num">{escape(str(latency)) + " ms" if latency is not None else "-"}</td></tr>'
        )
    return (
        '<div class="hop-frame"><table class="hop-table"><thead><tr><th scope="col">Service</th>'
        f'<th scope="col">Result</th><th scope="col" class="hop-num">Latency</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>'
    )


def _ranker_status_metrics(info: dict[str, Any], mode: str) -> list[str]:
    """Ranking index size, age and contexts, and which ranker this user is actually served."""
    from . import ranker

    videos = info.get("videos")
    serving_v2 = mode != "classic" and videos is not None
    label = ranker.MODE_LABELS.get(mode, mode)
    if mode != "classic" and not serving_v2:
        mode_value, mode_hint = "Classic", f"{label} chosen; index {info.get('state', 'not built')}"
    else:
        default_note = "default" if mode == ranker.DEFAULT_MODE else f"default is {ranker.MODE_LABELS[ranker.DEFAULT_MODE]}"
        mode_value, mode_hint = label, default_note
    age = info.get("age_seconds")
    age_text = f"{int(age // 60)} min old" if isinstance(age, (int, float)) and age >= 60 else (f"{int(age)} s old" if isinstance(age, (int, float)) else "")
    if videos is None:
        index_value, index_hint = "Not built", str(info.get("last_error") or info.get("state") or "")
    else:
        index_value = f"{int(videos):,} videos"
        index_hint = f"{int(info.get('contexts') or 0):,} related-list contexts, {age_text}"
    return [
        _status_metric("Ranker in use", mode_value, mode_hint),
        _status_metric("Ranking index", index_value, index_hint),
    ]


def _record_interleave_click(user_key: str, youtube_video_id: str) -> None:
    from . import ranker

    ranker.record_interleave_click(user_key, youtube_video_id)


def _interleave_status_metrics(summary: dict[str, Any] | None, enabled: bool) -> list[str]:
    """The "Help tune recommendations" readout: interleaved impressions, clicks per team, and
    the share of decided days Graph + popularity won, with its Wilson 95% interval."""
    shown = (summary or {}).get("impressions") or {}
    if not enabled and not sum(shown.values()):
        return []
    clicks = (summary or {}).get("clicks") or {}
    graph_shown, blend_shown = int(shown.get("graph") or 0), int(shown.get("blend") or 0)
    share = (summary or {}).get("blend_share")
    wins = (summary or {}).get("wins") or {}
    decided = int(wins.get("graph") or 0) + int(wins.get("blend") or 0)
    ties = int((summary or {}).get("ties") or 0)
    if share is None:
        preference, preference_hint = "No decided days", "a day counts once one side gets more opens"
    else:
        low, high = (summary or {}).get("blend_share_ci") or (0.0, 1.0)
        preference = f"{share:.0%} Graph + popularity"
        preference_hint = f"95% CI {low:.0%}–{high:.0%}, {decided} day{'' if decided == 1 else 's'} decided, {ties} tied"
    return [
        _status_metric("Tuning comparison", "On" if enabled else "Off", "Graph vs Graph + popularity, last 30 days"),
        _status_metric("Interleaved impressions", f"{graph_shown + blend_shown:,}", f"Graph {graph_shown:,} · Graph + popularity {blend_shown:,}"),
        _status_metric("Opens per side", f"{int(clicks.get('graph') or 0):,} / {int(clicks.get('blend') or 0):,}", "Graph / Graph + popularity"),
        _status_metric("Preference", preference, preference_hint),
    ]


def _status_page_html(user_key: str = "local") -> str:
    """Every subsystem, on one page, in the app's own visual language."""
    status = full_status_payload()
    checks = status.get("checks") or {}
    counts = status.get("counts") or {}
    backends = status.get("media_backends") or {}
    storage = backends.get("downloads") or {}
    sources = _status_source_rows(user_key)
    tone, headline, detail = _status_verdict(status, sources)

    # --- counters ------------------------------------------------------------------
    passing = sum(1 for key in _STATUS_CHECK_LABELS if (checks.get(key) or {}).get("ok"))
    in_use = [source for source in sources if source.get("enabled", True)]
    probe_ages = [source["health"]["last_checked"] for source in sources if source["health"].get("last_checked") is not None]
    counters = "".join(
        f'<div class="status-cell"><span class="stat-label">{label}</span><strong>{escape(str(value))}</strong></div>'
        for label, value in (
            ("Checks passing", f"{passing} of {len(_STATUS_CHECK_LABELS)}"),
            ("Sources OK", sum(1 for s in in_use if s["health"].get("status") == "healthy" and not s["cooling"])),
            ("Cooling down", sum(1 for s in in_use if s["cooling"])),
            ("Failing", sum(1 for s in in_use if s["failing"])),
            ("Last probe", f"{min(probe_ages)}s ago" if probe_ages else "None yet"),
        )
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

    try:
        from . import ranker

        ranker_status = ranker.status()
        _ranker_prefs = db.get_feed_preferences(user_key)
        ranker_mode_value = ranker.resolve_mode(_ranker_prefs.get("ranker_mode"))
        explore_on = bool(_ranker_prefs.get("explore_enabled"))
    except Exception as exc:  # noqa: BLE001 - the panel still renders
        logger.warning("status ranker read failed: %s", exc)
        ranker_status, ranker_mode_value, explore_on = {"state": f"unknown ({exc})"}, "classic", False
    try:
        interleave = ranker.interleave_summary()
    except Exception as exc:  # noqa: BLE001 - the readout is optional
        logger.info("interleave readout skipped: %s", exc)
        interleave = None
    hours = (watch.get("total_seconds") or 0) / 3600
    personalization = "".join([
        *_ranker_status_metrics(ranker_status, ranker_mode_value),
        _status_metric("Watch time tracked", f"{hours:.1f} h", f"{len(watch.get('channels') or {})} channel{'' if len(watch.get('channels') or {}) == 1 else 's'}"),
        _status_metric("Signal confidence", f"{(watch.get('confidence') or 0):.0%}", "full weight at 1 hour"),
        _status_metric("Imported history", f"{takeout_profile.get('total_watches') or 0:,}",
                       "Takeout" if takeout_record else "no import yet"),
        _status_metric("Already-watched IDs", f"{takeout_profile.get('watched_videos') or 0:,}", "suppressed in ranking"),
        *_interleave_status_metrics(interleave, explore_on),
    ])

    # --- media backends ------------------------------------------------------------
    ytdlp_version = backends.get("ytdlp") or ""
    ffmpeg_version = (backends.get("ffmpeg") or "").split(" version ")[-1].split(" ")[0] if backends.get("ffmpeg") else ""
    free = storage.get("free_bytes")
    media = "".join([
        _status_metric("yt-dlp", ytdlp_version or "not installed", "fallback + merged downloads"),
        _status_metric("ffmpeg", ffmpeg_version or "not installed", "muxes split tracks"),
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
        _status_metric(
            str(key).replace("_", " ").title(),
            f"{value:,}" if isinstance(value, int) else (_relative_iso(value) or value) if str(key).startswith("latest") else value,
        )
        for key, value in list(enrichment.items())[:6]
    )

    sync_runs = status.get("latest_sync_runs") or []
    sync_rows = "".join(
        f'<tr><td>{escape(str(run.get("status") or ""))}</td>'
        f'<td>{escape(_relative_time(run.get("started_at")) or "-")}</td>'
        f'<td>{escape(str(run.get("subscriptions_count") or 0))}</td>'
        f'<td>{escape(str(run.get("entries_count") or 0))}</td>'
        f'<td>{escape(_text_excerpt(str(run.get("message") or ""), 90))}</td></tr>'
        for run in sync_runs
    ) or "<tr><td colspan=5>No sync runs yet.</td></tr>"

    attempts = status.get("recent_retrieval_attempts") or []
    retrieval_rows = "".join(
        f'<tr><td>{escape(str(item.get("source") or ""))}</td>'
        f'<td class="{"bad-text" if item.get("status") != "success" else ""}">{escape(str(item.get("status") or ""))}</td>'
        f'<td>{escape(_relative_time(item.get("attempted_at")) or "-")}</td>'
        f'<td>{escape(str(item.get("youtube_video_id") or ""))}</td>'
        f'<td>{escape(_text_excerpt(str(item.get("message") or ""), 80))}</td></tr>'
        for item in attempts
    ) or "<tr><td colspan=5>No retrieval attempts recorded.</td></tr>"

    # Pretty-printed so a pasted report reads, and sorted so two reports diff cleanly.
    raw = json.dumps(status, indent=2, sort_keys=True, default=str)

    return f"""
      <div class="status-view">
        <header class="status-verdict is-{tone}">
          <i class="status-dot is-{tone}" aria-hidden="true"></i>
          <div class="status-verdict-copy">
            <h2>{escape(headline)}</h2>
            <p>{escape(detail)} Checked just now.</p>
          </div>
          <div class="status-verdict-actions">
            <button class="btn btn-sm" type="button" data-status-refresh>Refresh</button>
            <a class="btn btn-sm btn-ghost" href="/admin/status" data-status-page-link>Open as page</a>
          </div>
        </header>
        <div class="status-counters">{counters}</div>

        {_status_section("Source ladder", _status_ladder(sources), "Tried in this order. A cooled-down source is retried automatically.",
                         '<button class="btn btn-sm" type="button" data-open-settings-dialog data-settings-target="playback">Manage sources</button>')}
        {_status_section("Hop walk", _hop_walk_shell(), "Each hop of the private playback path, in order. The walk stops at the first break.",
                         '<button class="btn btn-sm" type="button" data-status-hops-run disabled>Run again</button>')}
        {_status_section("Services", _status_checks_table(checks), "The checks a working install needs.")}
        {_status_section("Media backends", f'<div class="stat-grid">{media}</div>')}
        {_status_section("Personalization", f'<div class="stat-grid">{personalization}</div>', "What the recommendation engine has to work with.")}
        {_status_section("Library", f'<div class="stat-grid">{library}</div>') if library else ""}
        {_status_section("Enrichment", f'<div class="stat-grid">{enrichment_tiles}</div>', "Metadata coverage. Topic ranking cannot see an unenriched video.") if enrichment_tiles else ""}
        {_status_fold("Recent syncs", len(sync_runs), f'<div class="status-scroll"><table><thead><tr><th>Status</th><th>Started</th><th>Subs</th><th>Entries</th><th>Message</th></tr></thead><tbody>{sync_rows}</tbody></table></div>')}
        {_status_fold("Recent retrieval attempts", len(attempts), f'<div class="status-scroll"><table><thead><tr><th>Source</th><th>Status</th><th>When</th><th>Video</th><th>Message</th></tr></thead><tbody>{retrieval_rows}</tbody></table></div>')}
        {_raw_output("Raw status output", raw)}
      </div>
    """


def _hop_walk_shell() -> str:
    """The hop table's slot. Filled by the script, which is what runs the walk."""
    return (
        '<div class="diagnosis" data-status-hops aria-live="polite"><div class="hop-frame" aria-hidden="true">'
        + '<div class="status-skeleton-row"><span class="skeleton skeleton-line w-40"></span></div>' * 4
        + '</div><span class="sr-only">Walking the playback chain</span></div>'
    )


def _status_skeleton() -> str:
    """The pane's shape while the checks run, so nothing moves when they land."""
    cells = (
        '<div class="status-cell"><span class="skeleton skeleton-line is-meta w-64"></span>'
        '<span class="skeleton skeleton-line w-40"></span></div>'
    ) * 5
    steps = '<div class="status-skeleton-row"><span class="skeleton skeleton-line w-40"></span></div>' * 3
    return (
        '<div class="status-view is-loading" role="status"><span class="sr-only">Loading status</span>'
        '<div aria-hidden="true"><div class="status-verdict"><span class="skeleton status-dot"></span>'
        '<div class="status-verdict-copy"><span class="skeleton skeleton-line w-64"></span>'
        '<span class="skeleton skeleton-line is-meta w-40"></span></div></div>'
        f'<div class="status-counters">{cells}</div>'
        f'<div class="status-block"><div class="hop-frame">{steps}</div></div>'
        f'<div class="status-block"><div class="hop-frame">{steps}</div></div></div></div>'
    )


@app.get("/admin/status", response_class=HTMLResponse)
def admin_status(request: Request):
    """Status as a page of its own, so the URL can be sent to someone and reloaded.

    This used to redirect to /?settings=status, which drew the feed and then a dialog over
    it, and dropped the address as soon as it opened. The checks still load on the page's
    own request to /api/status/panel, so a slow backend shows a skeleton rather than a
    blank tab, and both surfaces render the same markup. Access is decided by
    enforce_api_access before this runs.
    """
    auth_redirect = _login_required_redirect(request)
    if auth_redirect:
        return auth_redirect
    content = f'{_page_head("Status")}{admin_pages.nav("status")}<div class="status-page" data-status-body data-status-autoload>{_status_skeleton()}</div>'
    return _app_shell("Status", "status", content, request=request)



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
    "volume": '<path d="M11 5 6 9H2v6h4l5 4z"/><path d="M15.5 8.5a5 5 0 0 1 0 7"/><path d="M19 5a10 10 0 0 1 0 14"/>',
    "volume-x": '<path d="M11 5 6 9H2v6h4l5 4z"/><path d="m22 9-6 6"/><path d="m16 9 6 6"/>',
    "mic": '<path d="M12 19v3"/> <path d="M19 10v2a7 7 0 0 1-14 0v-2"/> <rect x="9" y="2" width="6" height="13" rx="3"/>',
    "plus": '<path d="M5 12h14"/> <path d="M12 5v14"/>',
    "pencil": '<path d="M21.174 6.812a1 1 0 0 0-3.986-3.987L3.842 16.174a2 2 0 0 0-.5.83l-1.321 4.352a.5.5 0 0 0 .623.622l4.353-1.32a2 2 0 0 0 .83-.497z"/> <path d="m15 5 4 4"/>',
    "block": '<circle cx="12" cy="12" r="10"/> <path d="M4.929 4.929 19.07 19.071"/>',
    "minus-circle": '<circle cx="12" cy="12" r="10"/> <path d="M8 12h8"/>',
    "queue": '<path d="M16 5H3"/> <path d="M11 12H3"/> <path d="M16 19H3"/> <path d="M18 9v6"/> <path d="M21 12h-6"/>',
    "calendar": '<path d="M8 2v4"/> <path d="M16 2v4"/> <rect width="18" height="18" x="3" y="4" rx="2"/> <path d="M3 10h18"/>',
    "shuffle": '<path d="m18 14 4 4-4 4"/> <path d="m18 2 4 4-4 4"/> <path d="M2 18h1.973a4 4 0 0 0 3.3-1.7l5.454-7.6a4 4 0 0 1 3.3-1.7H22"/> <path d="M2 6h1.972a4 4 0 0 1 3.6 2.2"/> <path d="M22 18h-6.041a4 4 0 0 1-3.3-1.8l-.359-.45"/>',
    "info": '<circle cx="12" cy="12" r="10"/> <path d="M12 16v-4"/> <path d="M12 8h.01"/>',
    "sort": '<path d="m21 16-4 4-4-4"/> <path d="M17 20V4"/> <path d="m3 8 4-4 4 4"/> <path d="M7 4v16"/>',
    "trash": '<path d="M10 11v6"/> <path d="M14 11v6"/> <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/> <path d="M3 6h18"/> <path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>',
    "live": '<path d="M4.9 19.1C1 15.2 1 8.8 4.9 4.9"/> <path d="M7.8 16.2c-2.3-2.3-2.3-6.1 0-8.5"/> <circle cx="12" cy="12" r="2"/> <path d="M16.2 7.8c2.3 2.3 2.3 6.1 0 8.5"/> <path d="M19.1 4.9C23 8.8 23 15.1 19.1 19"/>',
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


# History, bulk edit, sort, reorder, privacy and import add a few glyphs (library_tools.py).
ICON_PATHS.update(library_tools.ICON_PATHS)


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


def _count_label(count: Any, noun: str) -> str:
    """A count already formatted for display (1, 1.2K, 3,400) with its noun: 1 view, 2 views."""
    return f"{count} {noun}" if str(count) == "1" else f"{count} {noun}s"


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


def _relative_iso(value: Any) -> str:
    """_relative_time for a timestamp that has already been through JSON."""
    try:
        return _relative_time(datetime.fromisoformat(str(value)))
    except ValueError:
        return ""


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
    # Proxied through /ggpht/ or dropped: a stored or upstream avatar can still name Google.
    url = privacy.same_origin_image(avatar_url)
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
        parts.append(_count_label(views, "view"))
    relative = _relative_time(video.get("published_at"))
    if relative:
        parts.append(relative)
    return parts


SIDEBAR_VISIBLE_CHANNELS = 7


def _sidebar_channels(limit: int = 250, user_key: str = "") -> list[dict[str, Any]]:
    if not user_key:
        return []
    try:
        # Newest uploads first, in the query, so the capped probe holds the channels the
        # list leads with. Sorting after the LIMIT would only reorder seven random rows.
        rows = db.list_subscriptions(limit, recent_first=True, user_key=user_key)
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


def _sidebar_subscription_group(limit: int = SIDEBAR_VISIBLE_CHANNELS + 1, user_key: str = "") -> str:
    """Rendered on its own so /api/sidebar/subscriptions can swap it in after a
    subscription change without reloading the page.

    The shell renders only a SIDEBAR_VISIBLE_CHANNELS + 1 probe: enough to show the
    first screenful and to know whether more exist. The full list loads through
    /api/sidebar/subscriptions (limit=250) when it is actually expanded, so every
    page render does not pay for up to 750 subscription rows to draw seven links.
    """
    channels = _sidebar_channels(limit, user_key)
    if not channels:
        return ""
    rows = []
    # A label is only worth its height when it separates two groups, so "New" and
    # "Earlier" appear once and only when a row of that kind is actually rendered.
    group_shown = ""
    for index, channel in enumerate(channels):
        name = str(channel.get("title") or "Channel")
        channel_id = escape(str(channel.get("youtube_channel_id") or ""))
        is_new = _is_recent(channel.get("latest_video_at"))
        # The dot is not color-only: it carries sr-only "New" text so screen
        # readers announce fresh channels (aria-hidden would silence it).
        dot = '<span class="nav-dot"><span class="sr-only">New</span></span>' if is_new else ""
        avatar = _avatar_html(name, channel.get("channel_avatar_url") or channel.get("avatar_url"))
        # Everything past the first few is rendered but hidden, so "Show more" is instant
        # and needs no request. The shell only fetches a VISIBLE + 1 probe, so a capped
        # fetch means more may exist beyond what is rendered here — the count is exact
        # only when the fetch was not capped, and the full list arrives through
        # /api/sidebar/subscriptions when the group is expanded or refreshed.
        extra = " nav-channel-extra" if index >= SIDEBAR_VISIBLE_CHANNELS else ""
        hidden = " hidden" if index >= SIDEBAR_VISIBLE_CHANNELS else ""
        group = "New" if is_new else "Earlier"
        if group != group_shown:
            group_shown = group
            rows.append(f'<p class="nav-label{extra}"{hidden}>{group}</p>')
        rows.append(
            f'<a class="nav-item nav-channel{extra}" href="/channel/{channel_id}" title="{escape(name, quote=True)}"{hidden}>'
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
    return f'<nav class="nav-group nav-subs" data-subscription-nav aria-label="Subscribed channels">{"".join(rows)}{toggle}</nav>'


def _home_nav(active: str = "home", user_key: str = "") -> str:
    """The sidebar. Every entry goes to a page that *is* the thing it names.

    The old one kept sending you somewhere adjacent: History, Playlists and Liked all
    opened the Library page with a tab pre-selected, Collections opened the Subscriptions
    page, and Recommended opened Home — because `/recommendations` literally returned the
    home page. So clicking a thing landed you on a page that merely contained it, which
    is disorienting in a way that no amount of labelling fixes.

    Two entries are gone rather than repointed. "Library" was a container for four items
    that are all listed here individually, and "Recommended" was Home under another name.
    Both URLs still work and redirect, so old links and bookmarks survive.

    Three names cover what you follow, and each must be a different thing: Subscriptions
    is the feed of new uploads, Channels is the list of who you follow, and Collections
    is your own grouping of them. "All channels" used to open /channels, whose default
    view is another video timeline, which is why it read as a copy of Subscriptions. It
    now opens the channel list; the timeline view stays one click away on that page.
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
        ("Following", [
            ("channels", "Channels", "/channels?view=channels", "channels"),
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
            items.append(f'<a class="nav-item{item_class}" href="{href}" title="{escape(label, quote=True)}"{current}>{_icon(icon)}<span>{escape(label)}</span></a>')
        blocks.append(f'<nav class="nav-group">{"".join(items)}</nav>')

    blocks.append(_sidebar_subscription_group(user_key=user_key))
    return "".join(blocks)


def _mobile_nav(active: str = "home") -> str:
    """The phone tab bar: the four places people go daily, and You for everything else.

    History and Playlists used to hold two of five slots while Search, Liked, Downloads,
    Channels and Collections had no route at all at phone width. Search is a tab, so it
    sits under the thumb. You opens the account sheet, which lists every other destination,
    and stays lit while you are on one of them.
    """
    links = [
        ("home", "Home", "/", "home", ""),
        ("subscriptions", "Subscriptions", "/subscriptions", "subs", ""),
        ("shorts", "Shorts", "/shorts", "shorts", ""),
        ("search", "Search", "/search", "search", " data-search-open"),
    ]
    items = []
    for key, label, href, icon, attrs in links:
        current = ' aria-current="page"' if key == active else ""
        items.append(f'<a class="mobile-nav-item" href="{href}"{current}{attrs}>{_icon(icon)}<span>{escape(label)}</span></a>')
    you_current = ' aria-current="true"' if active in _YOU_TAB_ROUTES else ""
    items.append(f'<button class="mobile-nav-item" type="button" data-open-account{you_current}>{_icon("user")}<span>You</span></button>')
    return "".join(items)


_YOU_TAB_ROUTES = {"history", "playlists", "liked", "downloads", "channels", "collections", "status", "not-interested"}


def _topic_tokens_from_videos(videos: list[dict[str, Any]], limit: int = 7) -> list[str]:
    counts: dict[str, int] = {}
    for video in videos:
        for token in db._recommendation_tokens(video)[:6]:
            counts[token] = counts.get(token, 0) + 1
    return [token for token, _ in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]]


_HOME_FILTER_CHIPS: tuple[tuple[str, str], ...] = (
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
)

_HOME_FILTER_ALIASES = {
    "subscriptions": "subscription",
    "from-subscriptions": "subscription",
    "continue-watching": "continue",
    "recently-uploaded": "recent",
    "long": "long-form",
    "short": "short-videos",
    "shorts": "short-videos",
}


def _home_filter_key(raw: str) -> str:
    """A ?filter= value as a chip key. Anything unrecognised is plain Home."""
    key = str(raw or "").strip().lower()[:80]
    key = _HOME_FILTER_ALIASES.get(key, key)
    if key.startswith("topic:") and len(key) > 6:
        return key
    return key if any(key == chip for chip, _ in _HOME_FILTER_CHIPS) else "all"


def _topic_chips(videos: list[dict[str, Any]] | None = None, active: str = "all", user_key: str = "local") -> str:
    chips = list(_HOME_FILTER_CHIPS)
    existing = {key for key, _ in chips}
    seen_labels = {label.lower() for _, label in chips}
    dynamic: list[str] = []
    try:
        # Real YouTube tags describe videos far better than words pulled from titles.
        dynamic = [str(row.get("tag") or "") for row in db.list_popular_tags(8, user_key=user_key) if row.get("tag")]
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
    # A linked topic can fall out of the popular list; its chip still has to be there.
    if active.startswith("topic:") and active not in existing:
        chips.insert(1, (active, active.split(":", 1)[1].replace("-", " ").title()))
    items = []
    for key, label in chips:
        item_class = " active" if key == active else ""
        items.append(f'<button class="topic-chip{item_class}" type="button" data-filter="{escape(key, quote=True)}">{escape(label)}</button>')
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
        label = escape(name, quote=True)
        items.append(
            f'<a class="creator-pill" href="{href}" title="{label}" aria-label="{label}">{_avatar_html(name, video.get("channel_avatar_url"))}</a>'
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


def _watched_menu_row(video_id: str, watched: bool) -> str:
    """Mark watched, or Mark unwatched once it is. video_id is already escaped.

    The page script flips the row in place after either succeeds (shell.js), so the menu
    always offers the opposite of the video's current state.
    """
    event, label = ("mark_unwatched", "Mark unwatched") if watched else ("mark_watched", "Mark watched")
    return _menu_row("check", label, attrs=f' data-feedback="{event}" data-watched-toggle data-video-id="{video_id}"')


def _video_action_menu(video_id_raw: str, title_text: str, watch_href: str, original_url: str, invidious_url: str = "", extra_rows: str = "", channel_id: str = "", channel_title: str = "", watched: bool = False, reason: str = "") -> str:
    """The overflow menu on a video. extra_rows is trusted HTML: a list page's own actions.

    channel_id and channel_title are raw. With an id, the menu offers "Don't recommend
    channel", which mutes the creator the same way the feed controls dialog does.
    `reason` (raw) is the ranker's explanation; it heads the menu as "Why this video", so a
    phone, which has no hover title, can read it too (PRODUCT.md: ranking is inspectable).
    """
    video_id = escape(video_id_raw)
    why_row = _menu_row(
        "info", "Why this video", sub=reason,
        attrs=f' data-why-video="{escape(reason, quote=True)}"',
    ) if reason.strip() else ""
    mute_row = _menu_row(
        "minus-circle", "Don't recommend channel",
        attrs=f' data-mute-from-card data-channel-key="{escape(channel_id.lower(), quote=True)}" data-channel-title="{escape(_text_excerpt(channel_title, 60), quote=True)}"',
        danger=True,
    ) if channel_id else ""
    title = escape(_text_excerpt(title_text, 72))
    rows = [
        why_row,
        '<div class="menu-separator" role="separator"></div>' if why_row else "",
        _menu_row("play", "Watch in OpenFeed", href=watch_href),
        _menu_row("later", "Save to Watch later", attrs=f' data-watch-later="{video_id}"'),
        _menu_row("playlist", "Save to playlist", attrs=f' data-save-to-playlist="{video_id}" data-video-title="{escape(_text_excerpt(title_text, 90), quote=True)}"'),
        _watched_menu_row(video_id, watched),
        _menu_row("share", "Share", attrs=f' data-copy-url="{watch_href}"'),
        extra_rows,
        '<div class="menu-separator" role="separator"></div>',
        _menu_row("like", "More like this", attrs=f' data-feedback="more_like_this" data-video-id="{video_id}"'),
        _menu_row("dislike", "Less like this", attrs=f' data-feedback="less_like_this" data-video-id="{video_id}"'),
        _menu_row("block", "Not interested", attrs=f' data-feedback="hide" data-video-id="{video_id}"', danger=True),
        mute_row,
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
    here. A video pulled in by recommendations or search has no feed_entries row, so
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
          <div class="download-options" data-download-options role="radiogroup" aria-label="Quality"></div>
          <p class="download-warning" data-download-warning role="status" hidden></p>
          <div class="download-progress" data-download-progress hidden>
            <div class="download-progress-head">
              <strong data-download-progress-label>Preparing&hellip;</strong>
              <button class="btn btn-sm" type="button" data-download-cancel>Cancel</button>
            </div>
            <div class="download-bar"><span data-download-bar></span></div>
            <p class="dialog-note" data-download-stats></p>
          </div>
          <p class="dialog-note" data-download-note hidden></p>
        </div>
        <div class="dialog-actions" data-download-actions hidden>
          <button class="btn btn-ghost" type="button" data-close-dialog>Cancel</button>
          <button class="btn btn-primary" type="button" data-download-start>Download</button>
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
        # Unwrapped from YouTube's redirect and stripped of tracking parameters. The visible
        # text stays what the creator wrote; only where the click goes changes.
        href = escape(privacy.clean_outbound_url(unescape(trimmed)), quote=True)
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


# One line of a chapter list: "0:00 Intro", "[1:23] Title", "1:23 - Title", or the range
# form "00:00:00-00:17:13 Title". Anything else in a description is left alone.
_CHAPTER_LINE_RE = re.compile(
    r"^\s*[\[(]?((?:\d{1,2}:)?\d{1,2}:\d{2})[\])]?"
    r"(?:\s*[-\u2013\u2014]\s*(?:\d{1,2}:)?\d{1,2}:\d{2})?"
    r"[\s:|.\-\u2013\u2014)\]]*(.*?)\s*$"
)
_CHAPTER_TRAILING_RE = re.compile(r"^\s*(\S.*?)[\s:|\-\u2013\u2014(\[]+((?:\d{1,2}:)?\d{1,2}:\d{2})[\])]?\s*$")
_CHAPTER_MIN = 3
_CHAPTER_MIN_SECONDS = 10


def _parse_chapters(description: str, duration_seconds: Any = None) -> list[dict[str, Any]]:
    """Chapters as the creator wrote them, or none.

    Invidious's API has no chapter field, and YouTube itself derives them from the
    description, so the rule here is YouTube's: start at 0:00, at least three entries,
    ascending, at least ten seconds apart. A list that does not qualify is not chapters,
    and the page shows nothing rather than a guess.
    """
    duration = float(duration_seconds or 0)
    found: list[dict[str, Any]] = []
    for line in str(description or "").replace("\r\n", "\n").split("\n"):
        leading = _CHAPTER_LINE_RE.match(line)
        if leading:
            label, title = leading.group(1), leading.group(2)
        else:
            trailing = _CHAPTER_TRAILING_RE.match(line)
            if not trailing:
                continue
            label, title = trailing.group(2), trailing.group(1)
        title = title.strip(" \t-\u2013\u2014:|")
        if not title or int(label.rsplit(":", 1)[1]) > 59:
            continue
        start = _timestamp_seconds(label)
        if not found:
            if start == 0:
                found.append({"start": 0, "label": "0:00", "title": title[:120]})
            continue
        if start - found[-1]["start"] < _CHAPTER_MIN_SECONDS or (duration and start >= duration):
            continue
        found.append({"start": start, "label": _duration_text(start), "title": title[:120]})
    return found if len(found) >= _CHAPTER_MIN else []


def _comment_skeleton_rows(count: int = 3) -> str:
    return (
        '<div class="comment comment-skeleton" aria-hidden="true"><span class="skeleton skeleton-avatar"></span>'
        '<div><span class="skeleton skeleton-line is-meta"></span><span class="skeleton skeleton-line"></span></div></div>'
    ) * count


def _chat_panel(youtube_video_id: str, user_key: str = "local") -> str:
    """The chat panel, only with Live chat on in Settings. It stays hidden until
    /api/livechat says this video has live chat or a replay (assets/js/watch-media.js)."""
    try:
        if not db.get_playback_preferences(user_key).get("live_chat_enabled"):
            return ""
    except Exception:  # noqa: BLE001 - an optional panel never breaks the watch page
        return ""
    return (
        f'<details class="chapter-card chat-card" data-chat-panel data-video-id="{escape(youtube_video_id, quote=True)}" open hidden>'
        f'<summary class="chapter-head"><h2 data-chat-title>Chat</h2><span class="chat-state" data-chat-state role="status"></span>{_icon("chevron-down")}</summary>'
        '<ol class="chat-list" data-chat-list aria-label="Chat messages"></ol>'
        '<p class="chat-note" data-chat-note hidden></p>'
        '<p class="chat-foot">Read-only. Your server reads it through yt-dlp, so this page never contacts YouTube.</p>'
        '</details>'
    )


def _live_chat_settings_html(user_key: str = "local") -> str:
    """Settings, Integrations: the Live chat switch and what it sends."""
    if not ytdlp.available():
        return _set_group("Live chat", _set_row(
            "Live chat and chat replay",
            "Needs yt-dlp, which is not installed or is turned off on this server (OPENFEED_YTDLP_ENABLED).",
        ))
    try:
        on = bool(db.get_playback_preferences(user_key).get("live_chat_enabled"))
    except Exception:  # noqa: BLE001
        on = False
    return _set_group("Live chat", _toggle(
        "live_chat_enabled", "Live chat and chat replay", on,
        "A read-only chat panel on live streams and premieres, and the chat replay in step with the video on past streams.",
        "data-setting-live-chat",
    ), (
        '<p class="set-foot">Privacy trade-off: with this on, opening a video makes your server ask YouTube, through yt-dlp, '
        "whether that video has chat, and then read it. YouTube sees your server's address and the video ID, as it does "
        "when your Invidious plays the video, but no cookies, no account and nothing from your history. Your browser only "
        "talks to OpenFeed: author photos, custom emoji images and stickers are not loaded. Live chat is followed only while "
        "a watch page is open on it and stops within a minute of closing it. Replays are kept in memory, not saved. "
        "Nothing is ever posted to chat.</p>"
    ))


# Settings, Playback: caption languages offered by name. A stored code outside this list
# (from the player's own menu) is added to the options rather than dropped.
_CAPTION_LANGUAGES = (
    ("en", "English"), ("es", "Spanish"), ("fr", "French"), ("de", "German"), ("it", "Italian"),
    ("pt", "Portuguese"), ("nl", "Dutch"), ("sv", "Swedish"), ("pl", "Polish"), ("ru", "Russian"),
    ("uk", "Ukrainian"), ("tr", "Turkish"), ("ar", "Arabic"), ("hi", "Hindi"), ("id", "Indonesian"),
    ("vi", "Vietnamese"), ("ja", "Japanese"), ("ko", "Korean"), ("zh", "Chinese"),
)


def _speed_label(speed: float) -> str:
    return "Normal" if speed == 1 else f"{speed:g}x"


def _player_defaults_settings_html(user_key: str = "local") -> str:
    """Settings, Playback: what every video starts with. Stored with the playback
    preferences (db.PLAYBACK_DEFAULTS), saved on change by assets/js/playback-prefs.js."""
    try:
        prefs = db.get_playback_preferences(user_key)
    except Exception:  # noqa: BLE001 - the pane still opens with the defaults shown
        prefs = dict(db.PLAYBACK_DEFAULTS)
    speed = float(prefs.get("default_speed") or 1.0)
    language = str(prefs.get("captions_language") or "")
    languages = list(_CAPTION_LANGUAGES)
    if language.lower() in {code for code, _ in languages}:
        language = language.lower()
    elif language:
        languages.append((language, language))
    speed_select = _select(
        "default_speed", [(f"{value:g}", _speed_label(value)) for value in db.PLAYBACK_SPEEDS], f"{speed:g}",
        "Default playback speed", 'data-playback-pref="default_speed" data-pref-label="Default speed"',
    )
    language_select = _select(
        "captions_language", [("", "Your browser's language"), *languages], language,
        "Caption language", 'data-playback-pref="captions_language" data-pref-label="Caption language"',
    )
    rows = (
        _toggle("autoplay", "Autoplay next video", bool(prefs.get("autoplay")),
                "When a video ends, count down and play the first Up next video. Play all and Shuffle keep their own order. "
                "Also on the player bar.", 'data-playback-pref="autoplay" data-pref-label="Autoplay"')
        + _set_row("Default speed", "Every video starts at this speed. The speed menu in the player still changes it for one video.", speed_select)
        + _toggle("captions_enabled", "Always show captions", bool(prefs.get("captions_enabled")),
                  "Turn captions on whenever a video has them. Turning them on or off in the player changes this too.",
                  'data-playback-pref="captions_enabled" data-pref-label="Always show captions"')
        + _set_row("Caption language", "Preferred when a video has captions in several languages. Captions written by the creator come before automatic ones.", language_select)
    )
    return _set_group("Player", rows)


def _data_export_settings_html(user: dict[str, Any]) -> str:
    """Settings, Account: download everything this profile keeps (dataexport.py)."""
    name = str(_active_profile(user).get("name") or "") if user.get("profiles") else ""
    whose = f"Only {name}'s data." if name else "Only your data."
    return _set_group("Your data", _set_row(
        "Export your data",
        "One JSON file: subscriptions and collections, playlists, Watch later, liked videos, watch history with progress, "
        f"feedback such as Not interested, and settings. {whose}",
        f'<a class="btn" href="/api/export" download data-export-link>{_icon("download")}<span>Export</span></a>',
    ) + _set_row(
        "Subscriptions as OPML",
        "Your channel feeds in the format feed readers import.",
        f'<a class="btn" href="/api/export/subscriptions.opml" download data-export-link>{_icon("download")}<span>OPML</span></a>',
    ) + _set_row(
        "Import OPML",
        f"Subscribe {'this profile' if name else 'yourself'} to every feed in an OPML file from another feed reader or OpenFeed. Other profiles are not changed.",
        '<label class="btn opml-import" data-opml-import>'
        f'{_icon("plus")}<span>Choose file</span>'
        '<input class="sr-only" type="file" accept=".opml,.xml,text/x-opml,text/xml,application/xml" data-opml-file aria-label="Choose an OPML file to import">'
        '</label>',
    ))


def _watch_start_point(linked: Any, restart: bool, stored_resume: int) -> tuple[int, bool]:
    """Where a watch page starts, and whether that is a stored resume point to say so.

    A shared link's ?t= (90, 90s, 1m30s, 1:30) wins: it is where the sender meant the
    video to start, so no "Resuming from" either. ?restart=1 starts over.
    """
    if restart:
        return 0, False
    start = sharetime.parse_start(linked)
    if start is not None:
        return start, False
    return stored_resume, bool(stored_resume)


def _default_speed_attr(user_key: str) -> str:
    """The player shell's data-default-speed: "" at Normal, so nothing is applied."""
    try:
        speed = float(db.get_playback_preferences(user_key).get("default_speed") or 1.0)
    except Exception:  # noqa: BLE001 - a preference is never a reason the player fails
        return ""
    return "" if speed == 1 else f"{speed:g}"


def _watch_share_dialog(youtube_video_id: str, user_key: str = "local", short: bool = False) -> str:
    """The watch page's Share dialog, and the page's autoplay state beside it.

    YouTube's: the link in a field with Copy, and "Start at m:ss" to add the time the
    player is at. The link can point here or at YouTube, for someone without this server.
    assets/js/playback-prefs.js fills it in and runs autoplay off data-autoplay.
    """
    try:
        autoplay = bool(db.get_playback_preferences(user_key).get("autoplay"))
    except Exception:  # noqa: BLE001 - autoplay off is the safe reading of a failure
        autoplay = False
    video_id = escape(youtube_video_id, quote=True)
    return f'''
      <template data-watch-prefs data-autoplay="{"1" if autoplay and not short else "0"}"></template>
      <dialog class="feed-dialog share-dialog" data-share-dialog data-video-id="{video_id}" aria-labelledby="share-dialog-title">
        <div class="fc-head">
          <h2 id="share-dialog-title">Share</h2>
          <button class="icon-button" type="button" data-close-dialog aria-label="Close">{_icon("x")}</button>
        </div>
        <div class="share-body">
          {_segmented("share_target", [("openfeed", "OpenFeed link"), ("youtube", "YouTube link")], "openfeed", "Link to")}
          <div class="share-link">
            <input class="field-input" type="text" readonly data-share-url aria-label="Link to share" value="/watch/{video_id}">
            <button class="btn btn-primary" type="button" data-share-copy>Copy</button>
          </div>
          <div class="share-start">
            <label class="share-start-toggle"><input class="switch" type="checkbox" role="switch" data-share-start-on aria-describedby="share-start-time"><span>Start at</span></label>
            <input class="field-input share-start-time" id="share-start-time" type="text" inputmode="numeric" autocomplete="off" spellcheck="false" value="0:00" data-share-start-time aria-label="Start time, minutes and seconds" disabled>
          </div>
          <p class="share-error" data-share-error role="alert" hidden></p>
        </div>
      </dialog>'''


def _chapter_card(chapters: list[dict[str, Any]]) -> str:
    """The chapter list for the side panel. Every row is an ordinary seek button."""
    if not chapters:
        return ""
    rows = "".join(
        f'''<li><button class="chapter-row" type="button" data-seek="{int(chapter["start"])}" data-chapter-start="{int(chapter["start"])}" aria-label="Jump to {escape(str(chapter["label"]), quote=True)}, {escape(str(chapter["title"]), quote=True)}">'''
        f'''<span class="chapter-glyph" aria-hidden="true">{_icon("play")}</span>'''
        f'''<span class="chapter-time">{escape(str(chapter["label"]))}</span>'''
        f'''<span class="chapter-title">{escape(str(chapter["title"]))}</span></button></li>'''
        for chapter in chapters
    )
    return (
        f'<details class="chapter-card" open><summary class="chapter-head"><h2>Chapters</h2>'
        f'<span class="chapter-count">{len(chapters)}</span>{_icon("chevron-down")}</summary>'
        f'<ol class="chapter-list" data-chapter-list>{rows}</ol></details>'
    )


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
    """A kept video as a library row, with the file's own facts in place of the view count.

    _media_row always links to the watch page, which is what a download needs: the Shorts
    feed streams from Invidious and would ignore the copy on disk. The row action and
    the sheet row share a hook, so a phone gets the same delete without a target on the
    thumbnail.
    """
    video_id = escape(str(row.get("youtube_video_id") or ""), quote=True)
    title = str(row.get("title") or "Untitled")
    quality = str(row.get("quality") or "")
    chip = f'<span class="quality-chip">{escape(quality)}</span>' if quality else ""
    saved = _relative_time(row.get("downloaded_at"))
    meta = [part for part in (_bytes_text(row.get("size_bytes")), f"saved {saved}" if saved else "") if part]
    # Only rows that carry a stored path can be checked; absent means unknown, not missing.
    missing = bool(row.get("path")) and _download_file_path(row) is None
    trailing = _row_remove(f'data-remove-download="{video_id}"', f"Remove download: {title}")
    menu_rows = _menu_row("trash", "Remove download", attrs=f' data-remove-download="{video_id}"', danger=True)
    if missing:
        # The record outlived its file: offer the same quality again, kept on the server.
        again = (
            f' data-redownload="{video_id}" data-height="{int(row.get("height") or 0)}"'
            f' data-container="{escape(str(row.get("container") or "mp4"), quote=True)}" data-video-title="{escape(title, quote=True)}"'
        )
        trailing = (
            f'<button class="btn btn-sm redownload" type="button"{again} aria-label="Download again: {escape(title, quote=True)}">'
            f'{_icon("download")}<span>Download again</span></button>{trailing}'
        )
        menu_rows = _menu_row("download", "Download again", attrs=again) + menu_rows
    return _media_row(
        row, settings,
        meta=meta,
        note="The file is no longer on disk" if missing else "",
        note_danger=missing,
        thumb_overlay='<span class="quality-chip is-missing">File missing</span>' if missing else chip,
        trailing=trailing,
        menu_rows=menu_rows,
        variant="library",
        filter_text=f"{title} {row.get('channel_title') or row.get('author') or ''}",
    )


def _watch_options_menu(video_id_raw: str, invidious_url: str, youtube_url: str, title_text: str = "", watched: bool = False, can_download: bool = True) -> str:
    """The overflow menu for the action row.

    Download and Theater live here rather than on the row itself: YouTube keeps that row
    to the handful of actions people reach for constantly, and anything longer wraps onto
    a second line at ordinary window widths. Theater keeps its `t` shortcut either way.
    """
    video_id = escape(video_id_raw)
    title_attr = escape(_text_excerpt(title_text or video_id_raw, 90), quote=True)
    rows = [
        _menu_row("status", "Details", attrs=" data-open-details"),
        _menu_row("download", "Download", attrs=f' data-download-video="{video_id}" data-video-title="{title_attr}"') if can_download else "",
        # The shortcut hint is a span of its own, so without a name the row reads "Theater modet".
        _menu_row("theater", "Theater mode", trailing="t", attrs=' data-theater-toggle aria-label="Theater mode" aria-keyshortcuts="t"'),
        '<div class="menu-separator" role="separator"></div>',
        _watched_menu_row(video_id, watched),
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
        <summary class="pill-action pill-icon" aria-label="More actions" data-tip="More actions">{_icon("more")}<span class="sr-only">More actions</span></summary>
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
    auth_sub = str(user.get("sub") or "local")
    auth_status = ("Signed in" if user.get("session_backed") and not user.get("idp") else "Authentik account") if signed_in else ("Local mode" if not settings.require_login else "Login required")
    # Without login there is no person to name, and "LO" for "Local OpenFeed" says nothing.
    avatar_face = escape(_avatar_initials(display_name)) if signed_in else _icon("user")
    # With profiles, the face and the name are the profile's: that is who is watching.
    if user.get("profiles"):
        active_profile = _active_profile(user)
        avatar_face = _profile_avatar(active_profile, "fill")
        email = email or display_name if signed_in else ""
        display_name = str(active_profile.get("name") or display_name)
    # Account rows only exist where accounts do: with auth off there is nothing to sign
    # in to, and the user key is the constant "local".
    if signed_in:
        account_rows = account_pages.account_menu_rows(user) + _menu_row("external", "Sign out", href="/auth/logout") + _menu_row(
            "check", "User key", trailing=auth_sub[:22], attrs=" disabled"
        ).replace('class="menu-row"', 'class="menu-row disabled"')
    elif settings.auth_mode == "authentik_oidc":
        account_rows = _menu_row("user", "Sign in with Authentik", href="/auth/login?next=/")
    else:
        account_rows = ""
    account_block = f'<div class="menu-separator" role="separator"></div>{account_rows}' if account_rows else ""
    # Admin, Server holds everything install-wide; a member has no server pages to open.
    admin_row = _menu_row("status", "Admin", href="/admin/server") if not policy.is_member(user) else ""
    appearance_choice = _appearance_choice(user_key)
    return f'''
      <div class="header-actions">
        <details class="header-menu yt-menu" data-notifications>
          <summary class="icon-button bell-wrap" aria-label="Open notifications">{_icon("bell")}<span class="bell-badge" data-bell-badge{badge_hidden}>{badge_text}</span></summary>
          <div class="menu-panel menu-panel-wide notifications-panel" role="dialog" aria-label="Notifications">
            <div class="menu-heading"><span>Notifications</span><small data-notification-summary>From your subscriptions</small></div>
            <div class="notification-list" data-notification-list>{_notification_skeleton(4)}</div>
            <div class="notification-foot">
              <button class="notification-action" type="button" data-notifications-read hidden>Mark all read</button>
              <a class="notification-action" href="/subscriptions">All uploads</a>
            </div>
          </div>
        </details>
        <details class="header-menu profile-menu yt-menu">
          <summary class="icon-button profile-trigger" aria-label="Open OpenFeed account menu"><span class="profile-dot">{avatar_face}</span></summary>
          <div class="menu-panel account-panel" role="menu" aria-label="Account menu">
            <div class="account-summary"><span class="profile-dot">{avatar_face}</span><div><strong>{escape(display_name)}</strong><small>{escape(email or auth_status)}</small></div></div>
            {_profile_menu_block(user)}
            <div class="menu-separator" role="separator"></div>
            {_menu_row("history", "History", href="/history")}
            <div class="account-nav" role="none">
              {_menu_row("playlist", "Playlists", href="/playlists")}
              {_menu_row("like", "Liked videos", href="/liked")}
              {_menu_row("download", "Downloads", href="/downloads")}
            </div>
            {_menu_row("channels", "Channels", href="/channels?view=channels")}
            <div class="account-nav" role="none">
              {_menu_row("collections", "Collections", href="/channels?tab=collections")}
            </div>
            <div class="menu-separator" role="separator"></div>
            {_appearance_menu_row(appearance_choice)}
            {_menu_row("settings", "Settings", attrs=" data-open-settings-dialog")}
            {admin_row}
            {account_block}
            {_appearance_menu_page(appearance_choice)}
          </div>
        </details>
      </div>
    '''


def _notification_skeleton(count: int) -> str:
    """Placeholder rows for the bell panel, on the real row grid."""
    row = (
        '<div class="notification-row" aria-hidden="true"><span></span><span class="notification-copy">'
        '<span class="skeleton skeleton-line w-92"></span><span class="skeleton skeleton-line is-meta w-64"></span></span>'
        '<span class="notification-thumb skeleton"></span></div>'
    )
    return row * max(0, count)


def _set_row(title: str, note: str = "", control: str = "", attrs: str = "") -> str:
    """One setting: title and description on the left, the control on the right.

    title and note are plain text and are escaped here. control and attrs are trusted HTML.
    """
    note_html = f"<small>{escape(note)}</small>" if note else ""
    return (
        f'<div class="set-row"{attrs}><div class="set-copy"><strong>{escape(title)}</strong>{note_html}</div>'
        f'<div class="set-control">{control}</div></div>'
    )


def _set_group(label: str, rows: str, foot: str = "") -> str:
    """One panel per group. rows and foot are trusted HTML; the label is escaped."""
    return f'<section class="set-group"><h4 class="set-label">{escape(label)}</h4><div class="set-panel">{rows}</div>{foot}</section>'


def _fc_skeleton_rows(count: int) -> str:
    return '<div class="fc-signal-row" aria-hidden="true"><span class="skeleton skeleton-line w-64"></span></div>' * count


def _hidden_shelf_row(key: str) -> str:
    """One row of Feed controls → Hidden shelves. The page script builds the same row."""
    title = escape(_HIDEABLE_SHELVES.get(key, key))
    safe_key = escape(key, quote=True)
    return (
        f'<li class="fc-muted-row" data-hidden-shelf-row="{safe_key}"><span class="fc-muted-name">{title}</span>'
        f'<button class="btn btn-sm" type="button" data-show-shelf="{safe_key}" aria-label="Show {title} again">Show</button></li>'
    )


def _feed_dialog(user_key: str = "local") -> str:
    """Feed controls, built on what the feed actually contains.

    The previous version showed a "Subscriptions 35% / New to you 65%" bar derived from
    the slider position, a restatement of the input dressed up as a measurement, which is
    worse than showing nothing because it invites you to trust it. Every number here is
    counted from the ranked feed instead, so the panel answers "what am I actually being
    shown" before it offers to change it.

    Measuring means a full ranking pass, and this dialog is rendered into every Home load
    whether or not anyone opens it, so the measured panels load on open rather than here.
    """
    preferences = db.get_feed_preferences(user_key)
    preferred = ", ".join(preferences.get("preferred_topics") or [])
    muted = ", ".join(preferences.get("muted_topics") or [])
    discovery_level = max(0, min(100, int(float(preferences.get("discovery_level", 0.35)) * 100)))
    ranking_select = _ranker_mode_select(preferences.get("ranker_mode") or "")
    hidden_shelves = [key for key in preferences.get("hidden_shelves") or [] if key in _HIDEABLE_SHELVES]
    hidden_rows = "".join(_hidden_shelf_row(key) for key in hidden_shelves)

    return f"""
      <dialog class="feed-dialog fc-dialog" id="feed-dialog" aria-labelledby="feed-dialog-title">
        <form id="feed-preferences-form" method="dialog" data-tune-root>
          <div class="fc-head">
            <div>
              <p class="fc-kicker">Feed controls</p>
              <h2 id="feed-dialog-title" tabindex="-1" autofocus>What you see, and why</h2>
            </div>
            <button class="icon-button" type="button" data-close-dialog aria-label="Close feed controls">{_icon("x")}</button>
          </div>

          <div class="fc-body">
            <section class="fc-section">
              <h3>Your feed right now</h3>
              <div data-fc-composition><div class="fc-measure" aria-hidden="true"><span class="skeleton fc-bar"></span>
                <span class="skeleton skeleton-line is-meta w-92"></span></div><span class="sr-only" role="status">Measuring your feed</span></div>
            </section>

            <section class="fc-section">
              <h3>Signals in play</h3>
              <p class="fc-caption">What the ranker used, and how much of the feed each one touched.</p>
              <div class="fc-signals" data-fc-signals>{_fc_skeleton_rows(4)}</div>
            </section>

            <section class="fc-section">
              <h3>Ranking</h3>
              <label class="fc-field"><span>Recommendations</span>{ranking_select}</label>
              <p class="fc-caption">{escape(_ranker_mode_caption())}</p>
            </section>

            <section class="fc-section">
              <h3>How far to wander</h3>
              <div class="range-field">
                <label class="range-field-label" for="fc-discovery">Discovery</label>
                <output class="range-output" for="fc-discovery" data-suffix="%" data-tune-value>{discovery_level}%</output>
                <input class="fc-slider" id="fc-discovery" type="range" min="0" max="100" step="5" name="discovery_level"
                       value="{discovery_level}" data-tune-input>
                <div class="range-scale" aria-hidden="true"><span>Familiar</span><span>Balanced</span><span>Adventurous</span></div>
              </div>
              <p class="fc-caption">Low keeps close to what you already watch. High gives more slots to new
                uploads from related creators and a few to popular videos outside your links. This changes
                ranking only. The split above is measured after.</p>
            </section>

            <section class="fc-section">
              <h3>Sources</h3>
              <div class="set-panel">
                {_toggle("prioritize_subscriptions", "Prioritize subscriptions", bool(preferences.get("prioritize_subscriptions", True)), "Give channels you follow a head start: more of the feed comes from videos linked to them.")}
                {_toggle("exclude_shorts", "Exclude Shorts", bool(preferences.get("exclude_shorts", False)), "Hide videos under 90 seconds from every recommendation section.")}
              </div>
            </section>

            <section class="fc-section" data-fc-hidden-shelves{"" if hidden_rows else " hidden"}>
              <h3>Hidden shelves</h3>
              <p class="fc-caption">Shelves you hid from Home. Show one to bring it back on the next load.</p>
              <ul class="fc-muted-list" data-fc-hidden-list>{hidden_rows}</ul>
            </section>

            <section class="fc-section">
              <h3>Topics</h3>
              <label class="fc-field"><span>More of</span>
                <input class="field-input" name="preferred_topics" type="text" value="{escape(preferred, quote=True)}"
                       placeholder="woodworking, astronomy"></label>
              <label class="fc-field"><span>Less of</span>
                <input class="field-input" name="muted_topics" type="text" value="{escape(muted, quote=True)}"
                       placeholder="drama, reaction"></label>
              <p class="fc-caption">Comma separated, matched against each video&rsquo;s topic terms. Followed
                topics get a mild boost. Muted topics are left out (Classic gives them a heavy penalty instead).</p>
            </section>

            <section class="fc-section">
              <h3>Creators in your feed</h3>
              <p class="fc-caption">Muting removes a creator from recommendations entirely, not just
                lower in the feed. You stay subscribed. Mutes save as you press them.</p>
              <div class="fc-channels" data-fc-channels><span class="skeleton fc-channel-skeleton" aria-hidden="true"></span><span class="skeleton fc-channel-skeleton" aria-hidden="true"></span><span class="skeleton fc-channel-skeleton" aria-hidden="true"></span></div>
              <div class="fc-muted" data-fc-muted hidden>
                <h4 class="fc-muted-head">Muted creators</h4>
                <ul class="fc-muted-list" data-fc-muted-list></ul>
              </div>
            </section>
          </div>

          <div class="fc-foot">
            <p id="feed-preferences-status" class="status-line" role="status"></p>
            <button class="btn" type="button" data-reset-feed-preferences>Reset to defaults</button>
            <button class="btn btn-primary" type="submit">Save changes</button>
          </div>
        </form>
      </dialog>
    """


def _sponsorblock_settings_html(user_key: str = "local") -> str:
    """The SponsorBlock group: master switch, per-category action, and the trade stated."""
    from . import sponsorblock

    if not get_settings().sponsorblock_enabled:
        return _set_group("SponsorBlock", _set_row("SponsorBlock", "Disabled for this deployment."))
    try:
        preferences = db.get_feed_preferences(user_key)
        on = bool(preferences.get("sponsorblock_enabled"))
        actions = sponsorblock.normalize_actions(preferences.get("sponsorblock_actions"))
    except Exception:
        on, actions = False, dict(sponsorblock.DEFAULT_ACTIONS)

    rows = []
    for key, label, blurb in sponsorblock.CATEGORIES:
        select = _select(
            f"sponsorblock_{key}",
            [("skip", "Auto-skip"), ("manual", "Show skip button"), ("off", "Do nothing")],
            actions.get(key, "off"),
            f"{label} action",
            f'data-sponsorblock-category="{escape(key, quote=True)}"',
        )
        rows.append(_set_row(label, blurb, select))

    switch = _toggle(
        "sponsorblock_enabled", "SponsorBlock", on,
        "Skip sponsor reads and other interruptions using the community segment database.",
        "data-setting-sponsorblock",
    )
    privacy = (
        '<p class="set-foot">Privacy trade-off: this asks a third party about the videos you open, and it is kept as '
        "small as it can be. Lookups use SponsorBlock's hashed endpoint: OpenFeed sends only the first four characters "
        "of the video ID's SHA-256, and gets back segments for every video sharing that prefix, so SponsorBlock cannot "
        "tell which one you are watching. Requests go out from this server, never your browser, so your IP and session "
        "stay here, and results are cached so a video is asked about roughly once. This is read-only: OpenFeed never "
        "submits segments, votes, or reports, none of which are possible without identifying you.</p>"
    )
    categories = f'<div class="sb-categories" data-sponsorblock-categories{"" if on else " hidden"}>{"".join(rows)}</div>'
    return _set_group("SponsorBlock", switch + categories, privacy)


def _dearrow_settings_html(user_key: str = "local") -> str:
    """The DeArrow group: master switch, title casing, thumbnails, and the trade stated."""
    from . import dearrow

    if not get_settings().dearrow_enabled:
        return _set_group("DeArrow", _set_row("DeArrow", "Disabled for this deployment."))
    prefs = dearrow.preferences(user_key)
    on = prefs["enabled"]
    switch = _toggle(
        "dearrow_enabled", "Community titles and thumbnails", on,
        "Replace clickbait titles and thumbnails with ones submitted to DeArrow. Every replaced title keeps the original one step away.",
        "data-setting-dearrow",
    )
    casing = _set_row(
        "Title casing",
        "Lowercase is DeArrow's own lowercase format: every word, acronyms included. It applies to original titles too, as DeArrow does.",
        _select("dearrow_casing", [("", "As written"), ("lowercase", "lowercase")], prefs["casing"], "Title casing", "data-setting-dearrow-casing"),
    )
    thumbnails = _toggle(
        "dearrow_thumbnails", "Community thumbnails", prefs["thumbnails"],
        "DeArrow submits a moment of the video, not an image. This server cuts that frame itself with ffmpeg and keeps it cached.",
        "data-setting-dearrow-thumbnails",
    )
    privacy = (
        '<p class="set-foot">Privacy trade-off: this asks a third party about the videos on your screen, kept as small as '
        "SponsorBlock's. OpenFeed sends DeArrow only the first four characters of each video ID's SHA-256 and gets back "
        "titles for every video sharing that prefix, so DeArrow cannot tell which ones you were shown. Requests go out from "
        "this server, never your browser, and each prefix is cached for hours. Thumbnails never come from DeArrow's image "
        "service, which is asked by video ID: the frame is cut here from the stream your Invidious already plays, so the "
        "only party that sees it is YouTube, the same way it sees playback, and only for videos that have a submitted "
        "thumbnail. Read-only: OpenFeed never submits titles or votes.</p>"
    )
    options = f'<div class="sb-categories" data-dearrow-options{"" if on else " hidden"}>{casing}{thumbnails}</div>'
    return _set_group("DeArrow", switch + options, privacy)


def _api_tokens_lede(user: dict[str, Any]) -> str:
    if user.get("profiles"):
        name = str(_active_profile(user).get("name") or "this profile")
        return (f"Tokens let scripts and other services call the OpenFeed API as you. A token acts as the profile it "
                f"was created under, so these are {name}'s tokens and see {name}'s history, likes and playlists.")
    return "Tokens let scripts and other services call the OpenFeed API as you."


def _api_tokens_pane(user: dict[str, Any]) -> str:
    settings = get_settings()
    if user.get("via_token"):
        return _empty_state(
            "verified", "Tokens need a browser session",
            "An API token cannot create or revoke tokens. Open Settings in a signed-in browser.",
        )
    if not user.get("authenticated") and not _tokens_without_sign_in():
        # A token belongs to a signed-in account. Sign-in off but required locks every
        # caller out, so there is nobody to give one to.
        if settings.auth_mode != "authentik_oidc":
            return _empty_state(
                "verified", "API tokens are unavailable because sign-in is off",
                "This server requires sign-in (OPENFEED_REQUIRE_LOGIN) but has none turned on, so nobody can sign in "
                "and no token can be issued. Turn sign-in on, or stop requiring it to use tokens on the local profile.",
            ) + _set_group("To enable tokens", _set_row(
                "Turn on sign-in", "Add this to the .env file next to compose.yml with your Authentik client settings, then run ./deploy.sh.",
                "<code>OPENFEED_AUTH_MODE=authentik_oidc</code>",
            ))
        if not settings.auth_configured:
            missing = _auth_missing_envs()
            rows = "".join(_set_row(item, "Not set.") for item in missing)
            return _empty_state(
                "verified", "API tokens are unavailable until sign-in is configured",
                "Sign-in is selected on this server, but it is missing the settings below, so nobody can sign in to create a token.",
            ) + _set_group("Missing settings", rows)
        return _empty_state(
            "verified", "Sign in to manage API tokens",
            "A token is issued to your account, so it needs a signed-in session to create or revoke one.",
            '<a class="btn btn-primary" href="/auth/login?next=/%3Fsettings%3Dapi-tokens">Sign in</a>',
        )
    create = (
        '<form class="api-token-form" data-api-token-form>'
        '<div class="api-token-fields">'
        '<label class="text-field"><span class="set-label">Name</span><input name="name" required maxlength="120" autocomplete="off" placeholder="home-script"></label>'
        '<div class="text-field"><span class="set-label">Access</span>'
        f'{_segmented("access", [("read", "Read only"), ("write", "Read and write")], "read", "Token access")}</div>'
        '<button class="btn btn-primary" type="submit">Create token</button></div></form>'
    )
    open_note = (
        '<p class="set-foot" data-api-token-open-note>Sign-in is off on this install, so the API also answers calls that '
        "send no token, from anything that can reach this server: keep the port private. A token still names the profile "
        "a script acts as, and lets a client that asks for a key connect. Read only limits calls that send the token. "
        "Tokens made while sign-in is off stop working if it is turned on; create new ones then.</p>"
        if not user.get("authenticated") else ""
    )
    return f"""
      {_set_group("New token", create, open_note)}
      <div class="api-token-result" data-api-token-result hidden>
        <p class="set-label">New token</p>
        <input data-api-token-value readonly autocomplete="off" spellcheck="false" aria-label="New API token">
        <p class="dialog-note">Shown once. Store it now. It disappears when you press Done or close Settings, and it cannot be recovered.</p>
        <div class="settings-pane-actions"><button class="btn btn-primary" type="button" data-api-token-copy>Copy token</button><button class="btn" type="button" data-api-token-dismiss>Done</button></div>
      </div>
      <p class="status-line" role="status" aria-live="polite" data-api-token-status></p>
      <section class="set-group">
        <div class="set-label-row"><h4 class="set-label">Your tokens</h4><button class="btn btn-sm btn-ghost" type="button" data-api-token-refresh>Refresh</button></div>
        <div class="set-panel api-token-list" data-api-token-list aria-label="Your API tokens"></div>
      </section>
    """


# ---------------------------------------------------------------------------
# Appearance: Dark, Light, Use device theme. appearance.py has the model, theme.js the
# behaviour, 00-tokens.css the light token set.
# ---------------------------------------------------------------------------

ICON_PATHS.setdefault("moon", '<path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/>')


def _appearance_choice(user_key: str) -> str:
    """This user's stored choice, or "" when none (the head script then asks the browser)."""
    from . import appearance

    return appearance.get_choice(user_key)


def _appearance_menu_row(choice: str) -> str:
    """The account-menu row, "Appearance: Dark", that opens the three options."""
    from . import appearance

    label = appearance.SHORT_LABELS[choice or appearance.DEFAULT]
    return (
        '<button class="menu-row" type="button" role="menuitem" aria-haspopup="true" data-appearance-open>'
        f'<span class="menu-icon">{_icon("moon")}</span><span class="menu-copy"><span class="menu-label">'
        f'Appearance: <span data-appearance-label>{escape(label)}</span></span></span>'
        f'<span class="menu-trailing">{_icon("chevron-right")}</span></button>'
    )


def _appearance_menu_page(choice: str) -> str:
    """The page the row opens inside the same panel: back, a note, and the three options."""
    from . import appearance

    selected = choice or appearance.DEFAULT
    options = "".join(
        f'<button class="menu-row appearance-option" type="button" role="menuitemradio" data-appearance-option="{key}" '
        f'aria-checked="{"true" if key == selected else "false"}"><span class="menu-icon">{_icon("check")}</span>'
        f'<span class="menu-copy"><span class="menu-label">{escape(appearance.LABELS[key])}</span></span>'
        '<span class="menu-trailing"></span></button>'
        for key in ("system", "dark", "light")
    )
    return (
        '<div class="appearance-page" data-appearance-page role="group" aria-labelledby="appearance-page-title">'
        f'<div class="appearance-head"><button class="icon-button appearance-back" type="button" data-appearance-back aria-label="Back to account menu">{_icon("chevron-left")}</button>'
        '<h2 id="appearance-page-title">Appearance</h2></div>'
        '<p class="appearance-note">Device theme follows your system and changes with it. Saved to this profile.</p>'
        f"{options}</div>"
    )


def _appearance_pane(user_key: str) -> str:
    """Settings → Appearance: the same three choices as a radio group."""
    from . import appearance

    selected = _appearance_choice(user_key) or appearance.DEFAULT
    items = "".join(
        f'<label class="segment"><input class="sr-only" type="radio" name="appearance" value="{key}" data-appearance-option="{key}"'
        f'{" checked" if key == selected else ""}>{escape(appearance.SHORT_LABELS[key])}</label>'
        for key in ("system", "dark", "light")
    )
    control = f'<div class="segmented appearance-segmented" role="radiogroup" aria-label="Appearance">{items}</div>'
    return (
        '<p class="settings-lede">How OpenFeed looks on your screens. Saved to this profile and applied at once.</p>'
        + _set_group("Theme", _set_row(
            "Appearance", "Device theme follows your system's light or dark setting and changes with it. "
            "Videos and the player stay dark in every theme.", control,
        ))
    )


class AppearanceRequest(BaseModel):
    appearance: str


@app.get("/api/settings/appearance", tags=["settings"], summary="This user's theme choice")
def api_get_appearance(request: Request) -> dict[str, Any]:
    from . import appearance

    stored = appearance.get_choice(_user_key(request))
    return {"ok": True, "appearance": stored or appearance.DEFAULT, "stored": bool(stored), "choices": list(appearance.CHOICES)}


@app.put("/api/settings/appearance", tags=["settings"], summary="Set the theme: dark, light or system")
def api_set_appearance(request: Request, payload: AppearanceRequest) -> dict[str, Any]:
    from . import appearance

    if not appearance.normalize(payload.appearance):
        raise HTTPException(status_code=400, detail="appearance must be one of " + ", ".join(appearance.CHOICES))
    saved = appearance.save_choice(payload.appearance, _user_key(request))
    return {"ok": True, "appearance": saved}


@app.get("/settings", include_in_schema=False)
def settings_shortcut() -> RedirectResponse:
    return RedirectResponse(url="/?settings=api-tokens", status_code=303)


# ---------------------------------------------------------------------------
# Settings > Privacy: every outside service, who sees what, and the switches. The rules
# and the data live in privacy.py; assets/js/privacy.js saves the switches.
# ---------------------------------------------------------------------------

ICON_PATHS.setdefault("shield", '<path d="M12 3 5 6v5c0 4.5 3 8.3 7 10 4-1.7 7-5.5 7-10V6l-7-3Z"/>')

# The Integrations switch each server-side service already has. The Privacy pane mirrors it.
_PRIVACY_FEED_SWITCHES = {
    "sponsorblock_enabled": "[data-setting-sponsorblock]",
    "dearrow_enabled": "[data-setting-dearrow]",
    "ryd_enabled": "[data-setting-ryd]",
}


def _privacy_services(user_key: str) -> list[dict[str, Any]]:
    """privacy.services() for this user. A read failure shows everything off."""
    try:
        choices = privacy.get_choices(user_key)
        feed = db.get_feed_preferences(user_key)
        public_urls = list(db.get_playback_preferences(user_key).get("public_invidious_urls") or [])
    except Exception as exc:  # noqa: BLE001 - the pane must still open
        logger.info("privacy settings read failed: %s", exc)
        choices, feed, public_urls = dict(privacy.CHOICE_DEFAULTS), {}, []
    return privacy.services(choices, feed, public_urls)


def _privacy_service_row(service: dict[str, Any], admin: bool = False) -> str:
    key = escape(str(service["key"]), quote=True)
    name = escape(str(service["name"]))
    detail_id = f"privacy-detail-{key}"
    facts = (
        f'<dl class="privacy-facts" id="{detail_id}">'
        f'<div><dt>Who</dt><dd>{escape(str(service["who"]))}</dd></div>'
        f'<div><dt>Sees</dt><dd>{escape(str(service["sees"]))}</dd></div>'
        f'<div><dt>When</dt><dd>{escape(str(service["when"]))}</dd></div></dl>'
    )
    warning = f'<p class="privacy-warning">{escape(str(service["warning"]))}</p>' if service.get("warning") else ""
    who_decides = ""
    if not service.get("allowed"):
        control = '<span class="privacy-state">Off on this server</span>'
        # Who can change that: an admin, under Admin, Server (players or lookups section).
        section = "players" if service.get("where") == "browser" else "lookups"
        who_decides = (
            f'<p class="privacy-admin-note">You can allow it for this server under '
            f'<a href="/admin/server#admin-{section}">Admin, Server</a>.</p>' if admin else
            '<p class="privacy-admin-note">An admin of this OpenFeed can allow it for this server.</p>'
        )
    else:
        attr = "data-privacy-choice" if service["where"] == "browser" else "data-privacy-feed"
        mirror = _PRIVACY_FEED_SWITCHES.get(str(service["key"]), "")
        mirror_attr = f' data-privacy-mirror="{escape(mirror, quote=True)}"' if mirror else ""
        control = (
            f'<input class="switch" type="checkbox" role="switch" name="privacy_{key}" {attr}="{key}"{mirror_attr}'
            f'{" checked" if service.get("on") else ""} aria-label="{name}" aria-describedby="{detail_id}">'
        )
    state = "on" if service.get("on") else "off"
    return (
        f'<div class="set-row privacy-row" data-privacy-service="{key}" data-state="{state}">'
        f'<div class="set-copy"><strong>{name}</strong>{warning}{facts}{who_decides}</div>'
        f'<div class="set-control">{control}</div></div>'
    )


def _privacy_summary(services: list[dict[str, Any]]) -> str:
    on = [str(service["name"]) for service in services if service.get("on")]
    if not on:
        return "Nothing outside this server is on. Your browser talks only to this server."
    return "On for you: " + ", ".join(on) + "."


def _privacy_pane(user_key: str, admin: bool = False) -> str:
    """admin: the viewer may change the server's switches, so "Off on this server" links
    to Admin, Server; for everyone else it says an admin decides."""
    services = _privacy_services(user_key)
    browser = "".join(_privacy_service_row(item, admin) for item in services if item["where"] == "browser")
    server = "".join(_privacy_service_row(item, admin) for item in services if item["where"] == "server")
    proxy = outbound.proxy_label()
    always = (
        _set_row("YouTube, through this server",
                 "Feeds, video details, search, comments, captions and live chat are fetched by this server, mostly "
                 "through its own Invidious. YouTube sees this server's address, never yours.")
        + _set_row("Outbound proxy",
                   f"Requests from this server to YouTube and the services above leave through {proxy}." if proxy else
                   "Not set. Requests from this server to YouTube and the services above come from its own address.")
        + _set_row("Logs", "Your IP address and your searches are not written to this server's logs."
                   if not privacy.log_client_details() else
                   "Debug logging is on: this server's logs include IP addresses and full URLs, searches included.")
        + _set_row("Nothing else", "No analytics, no tracking, no update checks, no avatars from outside services, "
                   "no email. Thumbnails, avatars and video are proxied through this server.")
    )
    source = escape(get_settings().source_url, quote=True)
    return (
        '<p class="settings-lede">Your browser talks only to this server. These are the exceptions you can make, '
        'and what each one costs. All of them start off.</p>'
        f'<p class="privacy-summary" data-privacy-summary role="status" aria-live="polite">{escape(_privacy_summary(services))}</p>'
        + _set_group("Players from other sites", browser,
                     '<p class="set-foot">These connect your browser to someone else. OpenFeed never switches to one by '
                     "itself: when it cannot play a video privately, the player shows a button for each one you turned on "
                     "here, and nothing loads until you press it.</p>")
        + _set_group("Lookups this server makes for you", server,
                     '<p class="set-foot">These are asked by this server, not your browser, so they never see your IP '
                     "address or account. The same switches are under Integrations.</p>")
        + _set_group("Always", always,
                     f'<p class="set-foot">The full list of what goes where is in '
                     f'<a href="{source}/blob/main/PRIVACY.md" target="_blank" rel="noreferrer noopener">PRIVACY.md</a>.</p>')
    )


def _on_request_group(user_key: str) -> str:
    """Playback pane: the third-party players this user opted into, or nothing."""
    sources = _on_request_sources(user_key)
    if not sources:
        return ""
    rows = "".join(_set_row(str(source["label"]), str(source["warning"]),
                            '<span class="privacy-state">On request</span>') for source in sources)
    return _set_group("Only when you press Play from", rows,
                      '<p class="set-foot">Never tried automatically. When the sources above fail, the player offers '
                      'these as buttons. Turn them off under Privacy.</p>')


def _settings_dialog(user: dict[str, Any] | None = None, user_key: str = "local") -> str:
    settings = get_settings()
    user = user or _current_user(None)
    try:
        _preferences = db.get_feed_preferences(user_key)
        comments_on = bool(_preferences.get("show_comments"))
        # Stored as exclude_shorts; surfaced as the friendlier positive "Show Shorts".
        shorts_on = not _preferences.get("exclude_shorts")
        ryd_on = bool(_preferences.get("ryd_enabled"))
        posts_on = bool(_preferences.get("show_posts_in_feeds"))
        ranker_mode = str(_preferences.get("ranker_mode") or "")
        explore_on = bool(_preferences.get("explore_enabled"))
        kids_on = bool(_preferences.get("kids_profile"))
    except Exception:
        comments_on, shorts_on, ryd_on, posts_on, ranker_mode, explore_on, kids_on = False, True, False, False, "", False, False
    # A kids profile has both forced off server-side; the switches say so instead of lying.
    kids_lock = " disabled" if kids_on else ""
    shorts_note = "Off on kids profiles." if kids_on else "Include vertical Shorts, with their own shelf on Home. Off hides them everywhere."
    comments_note = "Off on kids profiles." if kids_on else "Load comments on watch pages through Invidious, only when you open a video."

    # The trade-off is stated in full next to the switch rather than buried in docs: this
    # is the one setting that sends anything about what is in your library off the box.
    ryd_group = _set_group("Return YouTube Dislike", _toggle(
        "ryd_enabled", "Estimated dislike counts", ryd_on,
        "Fetch public dislike estimates from Return YouTube Dislike, which YouTube itself stopped publishing in 2021.",
        "data-setting-ryd",
    ), (
        '<p class="set-foot">Privacy trade-off: turning this on tells a third party which video IDs your server is '
        "interested in. To keep that as small as possible, OpenFeed only ever reads from RYD and never submits to it, so "
        "your likes and dislikes stay in your own database. Most lookups run in the background on the sync schedule. "
        "Opening a video with no estimate yet does trigger one, but it is shuffled into a batch of unrelated videos that "
        "also need filling in, so RYD sees a group and cannot tell which one you are watching. If there is nothing to hide "
        "it among, the request waits for the next background pass instead. Everything goes out from the server, so RYD "
        "never sees your IP, account, or session, and each video is asked about once and then cached. Counts shown on "
        "videos are RYD's estimates, not figures published by YouTube.</p>"
    )) if get_settings().ryd_enabled else _set_group(
        "Return YouTube Dislike", _set_row("Estimated dislike counts", "Disabled for this deployment.")
    )
    signed_in = bool(user.get("authenticated"))
    sign_in_exists = settings.auth_mode == "authentik_oidc"
    display_name_raw = str(user.get("display_name") or "Local OpenFeed")
    display_name = escape(display_name_raw)
    email = escape(str(user.get("email") or ""))
    sub = escape(str(user.get("sub") or "local"))
    groups = user.get("groups") if isinstance(user.get("groups"), list) else []
    group_html = "".join(f"<span>{escape(str(group))}</span>" for group in groups[:8])
    auth_badge = "Signed in" if signed_in else "Local mode"
    if signed_in and user.get("session_backed") and not user.get("idp"):
        auth_copy = "Your history, likes, playlists, collections and feed preferences belong to this account alone."
    elif signed_in:
        auth_copy = "Using your Authentik account. Feedback, playlists, collections, and feed preferences are scoped to this account."
    elif sign_in_exists:
        auth_copy = "Using local fallback state. Sign in to connect this browser to your identity-provider account."
    else:
        auth_copy = "Sign-in is off on this install, so everything here belongs to one local profile."
    if signed_in:
        auth_action = '<a class="btn" href="/auth/logout">Sign out</a>'
    elif sign_in_exists:
        auth_action = '<a class="btn btn-primary" href="/auth/login?next=/">Sign in with Authentik</a>'
    else:
        auth_action = ""
    configured = "Configured" if settings.auth_configured else "Needs client settings"
    require_login = "Required" if settings.require_login else "Optional"
    required_group = escape(settings.required_group or "None")
    authorized = "Allowed" if _user_has_required_access(user) else "Missing group"
    rss_status = _rss_poller_status()
    if rss_status.get("ok"):
        last_run = _relative_iso(rss_status.get("last_run_at")) or "recently"
        rss_poller = f"Last sync {last_run}, {int(rss_status.get('entries_count') or 0):,} entries"
    else:
        rss_poller = f"Not synced: {rss_status.get('error') or 'no sync runs recorded'}"
    panes = [
        ("playback", "Playback", "play"),
        ("content", "Content", "home"),
        ("appearance", "Appearance", "moon"),
        ("integrations", "Integrations", "external"),
        ("privacy", "Privacy", "shield"),
        ("import", "Import", "download"),
        ("api-tokens", "API tokens", "verified"),
        ("profiles", "Profiles", "user"),
        ("account", "Account & security", "user"),
        ("services", "Services", "channels"),
        ("status", "Status", "status"),
    ]
    # Everything here is personal except Services, Status and the playback probes, which
    # are the server's. On a shared install a member does not get those (Admin, Server).
    server_view = not policy.is_member(user)
    if not server_view:
        panes = [pane for pane in panes if pane[0] not in {"services", "status"}]
    nav = "".join(
        f'<button class="settings-nav-item{" active" if index == 0 else ""}" type="button" role="tab" id="settings-tab-{key}" '
        f'aria-controls="settings-pane-{key}" aria-selected="{"true" if index == 0 else "false"}" tabindex="{"0" if index == 0 else "-1"}" '
        f'data-settings-pane="{key}">{_icon(icon)}<span>{escape(label)}</span><span class="settings-nav-chevron">{_icon("chevron-right")}</span></button>'
        for index, (key, label, icon) in enumerate(panes)
    )

    instance_note = (
        "Someone else's Invidious server sees your IP address and what you watch there. Instances added here are "
        "offered only after you turn on public instances under Privacy, and only as a button you press."
    )
    # Adding your own public instances is only possible when the admin allows it.
    instance_editor = (
        f'''<section class="set-group instance-editor" data-instance-editor hidden>
                <h4 class="set-label">Public instances</h4>
                <div class="set-panel">
                  <div class="set-row">
                    <div class="set-copy"><strong>Add an instance</strong><small>{escape(instance_note)}</small></div>
                    <div class="set-control instance-add">
                      <input class="field-input" type="url" placeholder="yewtu.be" data-instance-input aria-label="Add an Invidious instance" aria-describedby="instance-error">
                      <button class="btn" type="button" data-instance-add>{_icon("plus")}<span>Add</span></button>
                    </div>
                  </div>
                  <p class="instance-error" id="instance-error" data-instance-error role="alert" hidden></p>
                  <div class="topic-chip-list" data-instance-chips></div>
                </div>
              </section>'''
        if privacy.user_instances_allowed() else ""
    )

    def pane(key: str, body: str, active: bool = False) -> str:
        return (
            f'<section class="settings-pane{" active" if active else ""}" id="settings-pane-{key}" data-settings-pane-body="{key}" '
            f'role="tabpanel" aria-labelledby="settings-tab-{key}">{body}</section>'
        )

    identity = (
        f'<div class="set-row settings-identity"><span class="profile-dot lg">{escape(_avatar_initials(display_name_raw))}</span>'
        f'<div class="set-copy"><strong>{display_name}</strong><small>{email or auth_badge}</small>'
        f'{f"""<div class="signal-list">{group_html}</div>""" if group_html else ""}</div>'
        f'<div class="set-control">{auth_action}</div></div>'
    )
    account_rows = "".join(
        _set_row(label, control=value)
        for label, value in (
            ("Mode", escape(settings.auth_mode)), ("OIDC", configured), ("Login policy", require_login),
            ("Required group", f"<code>{required_group}</code>"), ("Access", authorized), ("Subject", f"<code>{sub}</code>"),
        )
    )
    health_rows = (
        _set_row("Test all sources", "Ask every enabled source for a real player, one at a time.",
                 '<button class="btn" type="button" data-playback-test>Test all sources</button>')
        + _set_row("Diagnose playback", "Walk the private playback path hop by hop and name the first break.",
                   '<button class="btn" type="button" data-playback-diagnose>Diagnose playback</button>')
    ) if server_view else ""
    if not server_view:
        # The sign-in configuration is the server's; a member keeps their own key.
        account_rows = _set_row("Subject", control=f"<code>{sub}</code>")
    return f"""
      <dialog class="feed-dialog settings-dialog" id="settings-dialog" aria-labelledby="settings-dialog-title" data-settings-view="list">
        <div class="settings-shell">
          <aside class="settings-nav">
            <div class="settings-nav-head">
              <h2 id="settings-dialog-title">Settings</h2>
              <button class="icon-button settings-list-close" type="button" data-close-dialog aria-label="Close settings">{_icon("x")}</button>
            </div>
            <div class="settings-tabs" role="tablist" aria-label="Settings sections" aria-orientation="vertical">{nav}</div>
            <div class="settings-nav-foot">
              <span class="profile-dot">{escape(_avatar_initials(display_name_raw))}</span>
              <div><strong>{display_name}</strong><small>{email or auth_badge}</small></div>
            </div>
          </aside>
          <div class="settings-content">
            <header class="settings-topbar">
              <button class="icon-button settings-back" type="button" data-settings-back aria-label="Back to settings sections">{_icon("chevron-left")}</button>
              <h3 data-settings-title>Playback</h3>
              <button class="icon-button settings-close" type="button" data-close-dialog aria-label="Close settings">{_icon("x")}</button>
            </header>
            <div class="settings-scroll" data-settings-scroll tabindex="-1">
            {pane("playback", f"""
              {_player_defaults_settings_html(user_key)}
              <p class="settings-lede">OpenFeed plays from the first source that works and moves down the list when one fails. Reorder them, or turn a source off entirely.</p>
              <section class="set-group">
                <h4 class="set-label">Sources</h4>
                <div class="set-panel source-list" data-playback-sources aria-label="Playback sources">
                  {'<div class="source-row" aria-hidden="true"><span class="skeleton skeleton-line w-64"></span></div>' * 3}
                </div>
              </section>
              {_on_request_group(user_key)}
              {instance_editor}
              {_set_group("Health", health_rows) if health_rows else ""}
              <div class="playback-progress" data-playback-progress hidden><span data-playback-progress-fill></span></div>
              <p class="status-line" role="status" aria-live="polite" data-playback-status></p>
              <div class="playback-results" data-playback-results hidden></div>
              <div class="diagnosis" data-playback-diagnosis hidden></div>
            """, active=True)}
            {pane("content", f"""
              <p class="settings-lede">What shows up in your feed and on watch pages.</p>
              {_set_group("Show", _toggle("show_shorts", "Show Shorts", shorts_on, shorts_note, "data-setting-shorts" + kids_lock)
                + _toggle("show_comments", "Show comments", comments_on, comments_note, "data-setting-comments" + kids_lock)
                + _toggle("show_posts_in_feeds", "Show posts in feeds", posts_on, "Mix community posts from subscribed channels into Home and Subscriptions. The Posts tab on a channel works either way.", "data-setting-posts"))}
              {_set_group("Recommendations", _set_row("Ranking", _ranker_mode_caption(), _ranker_mode_select(ranker_mode, 'data-setting-ranker'))
                + _toggle("explore_enabled", "Help tune recommendations", explore_on,
                          "Mix Graph and Graph + popularity picks into the Recommended shelf and count which side you open more. "
                          "The result is on the Status page.", "data-setting-explore")
                + _set_row("Not interested", "Videos and channels you asked to see less of. Remove one to let it back in.",
                           '<a class="btn btn-sm" href="/not-interested">Review</a>'),
                '<p class="set-foot">Stays on this server: which shelf slot came from which ranking and whether you opened it '
                "are written to the impressions table, next to the impressions already kept for rotation. Nothing is sent "
                "anywhere. Excluded videos stay excluded, and one day&rsquo;s pages keep the same order. Applies to the "
                "Graph and Graph + popularity modes, not Classic.</p>")}
            """)}
            {pane("appearance", _appearance_pane(user_key))}
            {pane("integrations", f"""
              <p class="settings-lede">Off by default. Each one reaches a third party, so each states what it sends.</p>
              {_sponsorblock_settings_html(user_key)}
              {_dearrow_settings_html(user_key)}
              {ryd_group}
              {_live_chat_settings_html(user_key)}
            """)}
            {pane("privacy", _privacy_pane(user_key, admin=_user_is_admin(user)))}
            {pane("import", _takeout_pane_html(user_key))}
            {pane("api-tokens", f"""
              <p class="settings-lede">{escape(_api_tokens_lede(user))}</p>
              {_api_tokens_pane(user)}
            """)}
            {pane("profiles", _profiles_pane(user))}
            {pane("account", f"""
              <p class="settings-lede">{escape(auth_copy)}</p>
              <div class="set-panel">{identity}</div>
              {account_pages.settings_rows(user)}
              {admin_pages.settings_rows(user)}
              {_data_export_settings_html(user)}
              {_set_group("Sign-in", account_rows)}
            """)}
            {pane("services", f"""
              <p class="settings-lede">The backends OpenFeed depends on. Status has the live checks.</p>
              {_set_group("Backends", _set_row("RSS poller", rss_poller) + _set_row("Invidious", settings.invidious_public_url or "Not configured"))}
            """) if server_view else ""}
            {pane("status", f'<div data-status-body>{_status_skeleton()}</div>') if server_view else ""}
            </div>
          </div>
        </div>
      </dialog>
      <script src="/assets/api-tokens-v1.js" defer></script>
    """


# playback_progress calls a video finished at this share, so a card must agree with it.
_WATCHED_FRACTION = 0.85


def _progress_fraction(video: dict[str, Any]) -> float:
    """How far through the video you are, 0 to 1, which is how progress is stored.

    Values over 1 are read as percentages, because older rows and hand-seeded data use
    that scale. Reading a fraction as a percentage is why every rail used to draw at the
    2% minimum: 0.5 was taken for half of one percent.
    """
    try:
        value = float(video.get("percent_completed") or 0)
    except (TypeError, ValueError):
        return 0.0
    if value <= 0:
        return 0.0
    return min(1.0, value / 100 if value > 1 else value)


def _is_watched(video: dict[str, Any]) -> bool:
    if video.get("progress_completed") or video.get("completed") or video.get("manually_marked_watched"):
        return True
    return _progress_fraction(video) >= _WATCHED_FRACTION


def _progress_rail(video: dict[str, Any]) -> str:
    """The thumbnail's progress state: a bar while unfinished, a check once watched."""
    if _is_watched(video):
        return f'<span class="watched-mark" aria-hidden="true">{_icon("check")}</span>'
    fraction = _progress_fraction(video)
    if fraction <= 0:
        return ""
    return f'<span class="progress-rail" aria-hidden="true"><span style="width:{max(2, fraction * 100):.0f}%"></span></span>'


def _time_left_text(video: dict[str, Any]) -> str:
    """"12m left" for a video in progress, or "" when no duration is known.

    resume_seconds is where playback will actually restart, so it beats the percentage,
    which holds the furthest point ever reached.
    """
    if _is_watched(video):
        return ""
    fraction = _progress_fraction(video)
    if fraction <= 0:
        return ""
    duration = _video_duration(video)
    if duration <= 0:
        return ""
    try:
        position = float(video.get("resume_seconds") or 0)
    except (TypeError, ValueError):
        position = 0.0
    left = max(0.0, duration - (position if position > 0 else fraction * duration))
    minutes = round(left / 60)
    if minutes < 1:
        return "Under 1m left"
    if minutes < 60:
        return f"{minutes}m left"
    hours, rest = divmod(minutes, 60)
    return f"{hours}h {rest}m left" if rest else f"{hours}h left"


def _card_quick_actions(video_id_raw: str, title_text: str, watched: bool) -> str:
    """Hover and focus shortcuts on a card. Both repeat a row in its overflow menu.

    Only what the backend really does. Watch later is the playlist of that name, made on
    first save (search_extras); the clock toggles it and assets/js/search-home.js keeps its
    state. Touch never sees these, which is why each one also has to stay in the menu.
    """
    if not video_id_raw:
        return ""
    video_id = escape(video_id_raw, quote=True)
    title_attr = escape(_text_excerpt(title_text, 90), quote=True)
    mark = "" if watched else (
        f'<button class="card-quick-button" type="button" data-feedback="mark_watched" data-video-id="{video_id}" '
        f'aria-label="Mark watched" title="Mark watched">{_icon("check")}</button>'
    )
    save = (
        f'<button class="card-quick-button" type="button" data-save-to-playlist="{video_id}" data-video-title="{title_attr}" '
        f'aria-label="Save to playlist" title="Save to playlist">{_icon("playlist")}</button>'
    )
    later = (
        f'<button class="card-quick-button" type="button" data-watch-later="{video_id}" aria-pressed="false" '
        f'aria-label="Save to Watch later" title="Watch later">{_icon("later")}</button>'
    )
    return f'<div class="card-quick">{later}{mark}{save}</div>'


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
    original_url = escape(_safe_external_href(video.get("url")) or f"https://www.youtube.com/watch?v={video_id_raw}")
    invidious_url = escape(f"{settings.invidious_public_url}/watch?v={video_id_raw}") if video_id_raw and settings.invidious_public_url else ""
    thumbnail = escape(_thumbnail_for(video))
    thumb_img = f'<img src="{thumbnail}" alt="" loading="lazy" onerror="this.closest(\'.thumb\').classList.add(\'no-thumb\'); this.remove()">' if thumbnail else ""
    duration = escape(_duration_text(video.get("duration_seconds")))
    duration_html = f'<span class="duration">{duration}</span>' if duration else ""
    progress_html = _progress_rail(video)
    source = escape(str(video.get("source_kind") or "subscription"))
    source_label = escape(_source_label(video))
    meta_parts = _video_meta_parts(video)
    if video.get("stream_kind"):
        meta_parts, duration_html = _stream_card_parts(video, meta_parts, duration_html)
    time_left = _time_left_text(video)
    if time_left:
        # Time left is what you need to decide on a half-watched video, not its view count.
        meta_parts = [time_left, *[part for part in meta_parts if not part.endswith(" views")]]
    meta = " &middot; ".join(escape(part) for part in meta_parts)
    watched = _is_watched(video)
    watched_class = " is-watched" if watched else ""
    thumb_label = f"Watch {title}, watched" if watched else f"Watch {title}"
    verified = _icon("verified").replace('class="icon"', 'class="icon verified"') if video.get("source_kind") != "discovery" else ""
    recent = "true" if _is_recent(video.get("published_at")) else "false"
    duration_value = int(video.get("duration_seconds") or video.get("stored_duration_seconds") or 0)
    duration_class = "short" if 0 < duration_value <= 90 else "long" if duration_value >= 1200 else "standard"
    topic_tokens = " ".join(str(token) for token in (video.get("topic_tokens") or db._recommendation_tokens(video))[:8])
    reason_text = str(video.get("recommendation_reason") or "")
    menu = _video_action_menu(video_id_raw, title_text, watch_href, original_url, invidious_url, channel_id=str(video.get("youtube_channel_id") or ""), channel_title=channel_text, watched=watched, reason=reason_text)
    quick = _card_quick_actions(video_id_raw, title_text, watched)
    reason_attr = f' data-reason="{escape(reason_text, quote=True)}"' if reason_text else ""
    reason_title = f' title="{escape(reason_text, quote=True)}"' if reason_text else ""
    common_attrs = f'data-video-card data-video-id="{video_id}" data-channel-id="{channel_id}" data-source="{source}" data-recent="{recent}" data-duration-class="{duration_class}" data-topics="{escape(topic_tokens, quote=True)}"{reason_attr}'
    if variant == "mini":
        return f'''
        <article class="video-card video-card-mini{watched_class}" {common_attrs}>
          <a class="thumb" href="{watch_href}" aria-label="{thumb_label}"><span class="thumb-fallback">OpenFeed</span>{thumb_img}{duration_html}{progress_html}</a>
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
    <article class="{card_class}{watched_class}" {common_attrs}>
      <a class="thumb" href="{watch_href}" aria-label="{thumb_label}">
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
          <p class="metadata"{reason_title}>{meta or source_label}</p>
        </div>
      </div>
      {quick}
    </article>
    '''


def _view_all_link(href: str, title: str) -> str:
    """The way out of a shelf. Rendered only when there is a page that shows the whole set."""
    if not href:
        return ""
    return (
        f'<a class="view-all" href="{escape(href, quote=True)}" aria-label="View all: {escape(title, quote=True)}">'
        f'<span>View all</span>{_icon("chevron-right")}</a>'
    )


def _shelf_nav_buttons() -> str:
    return (
        f'<button class="shelf-nav prev" type="button" data-shelf-prev aria-label="Scroll left" disabled>{_icon("chevron-left")}</button>'
        f'<button class="shelf-nav next" type="button" data-shelf-next aria-label="Scroll right">{_icon("chevron-right")}</button>'
    )


def _shelf_html(
    shelf_id: str,
    title: str,
    videos: list[dict[str, Any]],
    settings: Any,
    dismissible: bool = True,
    eyebrow: str = "",
    kind: str = "",
    view_all: str = "",
) -> str:
    cards = "".join(_home_video_card(video, index, settings, "featured") for index, video in enumerate(videos, start=1))
    shelf_key = _shelf_key(shelf_id)
    # Only shelves that can be listed under Feed controls → Hidden shelves get the button:
    # hiding is stored per user and has to be reversible from somewhere other than Undo.
    close_button = (
        f'<button class="icon-button shelf-dismiss" type="button" data-dismiss-shelf="{escape(shelf_key, quote=True)}" '
        f'data-shelf-title="{escape(_HIDEABLE_SHELVES[shelf_key], quote=True)}" aria-label="Hide this shelf">{_icon("x")}</button>'
        if dismissible and shelf_key in _HIDEABLE_SHELVES else ""
    )
    kind_attr = f' data-section-kind="{escape(kind)}"' if kind else ""
    eyebrow_html = f'<p class="section-eyebrow">{escape(eyebrow)}</p>' if eyebrow else ""
    return f'''
    <section class="section-block feed-section" data-feed-section="{escape(shelf_id)}"{kind_attr} aria-labelledby="shelf-{escape(shelf_id)}">
      <div class="shelf" data-shelf>
        <div class="shelf-head">
          <div class="section-heading-copy">
            {eyebrow_html}
            <h2 id="shelf-{escape(shelf_id)}">{escape(title)}</h2>
          </div>
          <div class="shelf-actions">{_view_all_link(view_all, title)}{_shelf_nav_buttons()}{close_button}</div>
        </div>
        <div class="shelf-viewport">
          <div class="shelf-track" data-shelf-track>{cards}</div>
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
    """Subscription videos worth catching up on: ones this user has not started.

    Watch progress is per user. The old feed reader's install-wide "unread" flag used to
    be preferred here, so one person's reading reordered everyone's shelf; it is gone.
    """
    rows = pool if pool is not None else db.list_subscription_recommendations(max(32, limit * 4), user_key)
    return [row for row in rows if not row.get("percent_completed")][:limit]


def _without_shorts(videos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Shorts belong in the Shorts shelf and the Shorts player, not in a 16:9 grid."""
    return [video for video in videos if not db.is_short_video(video)]


def _without_live(videos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Live streams and premieres (stored with a duration of exactly 0) are not picks.

    Ranker v2 drops them itself. The older lanes deeper in Home (recent, long-form) do not,
    and live rows are about 16% of the library, so without this they filled those grids.
    An unknown duration (None) is not treated as live.
    """
    return [video for video in videos if video.get("duration_seconds") is None or float(video.get("duration_seconds") or 0) > 0]


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
    {"key": "unread", "id": "in-case-you-missed", "eyebrow": "Catch up", "title": "In Case You Missed", "layout": "featured", "view_all": "/subscriptions"},
    {"key": "week", "id": "new-this-week", "eyebrow": "This week", "title": "New This Week From Your Subscriptions", "layout": "grid", "view_all": "/subscriptions"},
    {"key": "older", "id": "while-you-were-away", "eyebrow": "Backlog", "title": "While You Were Away", "layout": "featured"},
    {"key": "quick", "id": "quick-catch-up", "eyebrow": "Short on time", "title": "Quick Catch-Up", "layout": "grid"},
    {"key": "regulars", "id": "your-regulars", "eyebrow": "Your regulars", "title": "Back to the Channels You Watch Most", "layout": "featured"},
    {"key": "long", "id": "long-watch-catch-up", "eyebrow": "Deep watch", "title": "Long Watches You Missed", "layout": "compact"},
]

# The Home shelves a user can hide ("Hide this shelf"), by stable key, with the name Feed
# controls lists them under. Section ids carry a page suffix on deeper pages; the key does not.
_HIDEABLE_SHELVES: dict[str, str] = {
    "continue-watching": "Continue watching",
    "new-subscriptions": "New from your subscriptions",
    **{shelf["id"]: shelf["title"] for shelf in _CATCH_UP_SHELVES if shelf["layout"] == "featured"},
}


def _shelf_key(section_id: str) -> str:
    return re.sub(r"-\d+$", "", str(section_id or ""))


def _without_hidden_shelves(sections: list[dict[str, Any]], user_key: str) -> list[dict[str, Any]]:
    """Leave out the shelves this user hid. Home must render regardless, so a failed read hides nothing."""
    try:
        hidden = set(db.get_feed_preferences(user_key).get("hidden_shelves") or [])
    except Exception as exc:  # noqa: BLE001 - a preference read must not fail the page
        logger.info("hidden shelves read failed: %s", exc)
        return sections
    if not hidden:
        return sections
    return [section for section in sections if _shelf_key(section.get("id", "")) not in hidden]


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


CATCH_UP_SIZE = 8
# Page 0's "New from your subscriptions" shelf reads the head of the same pool. Its ids are
# held back from every later catch-up shelf; a few spare slots cover the videos it skips
# because the page above it already showed them.
_CATCH_UP_SUBSCRIPTION_HEAD = 16


def _is_catch_up_page(page: int, seed: int) -> bool:
    """Page 0 always carries a catch-up shelf; deeper pages one in three."""
    return page == 0 or (page + seed) % 3 == 0


def _catch_up_plan(pool: list[dict[str, Any]], user_key: str, seed: int, page: int) -> dict[int, dict[str, Any]]:
    """Which catch-up shelf, and which videos, every catch-up page up to `page` gets.

    Each Home page is its own request, so a shelf cannot remember what an earlier page
    showed. It used to pick its variant as `(seed + 2 * page) % 6`, and catch-up pages are
    three apart, so every one landed on the same variant and showed the head of the same
    slice: "While You Were Away" with the same eight videos every third page.

    The plan replays the whole load instead. It is a pure function of the pool, the seed
    and the page, so page 7 recomputes exactly what pages 0 and 4 chose. The k-th catch-up
    shelf of a load starts at variant `seed + k`, and every shelf takes the next videos of
    its slice that no earlier shelf, and not page 0's subscription shelf, used. The page's
    own duplicates are removed afterwards (see _catch_up_section), never during the
    replay, so the replay never depends on what the rest of a page held.
    """
    if not pool:
        return {}
    count = len(_CATCH_UP_SHELVES)
    candidates: dict[str, list[dict[str, Any]]] = {}

    def slice_for(key: str) -> list[dict[str, Any]]:
        if key not in candidates:
            candidates[key] = _catch_up_videos(key, pool, user_key, limit=len(pool))
        return candidates[key]

    used: set[str] = set()
    plan: dict[int, dict[str, Any]] = {}
    occurrence = 0
    for current in range(0, max(0, page) + 1):
        if not _is_catch_up_page(current, seed):
            continue
        start = (seed + occurrence) % count
        occurrence += 1
        for step in range(count):
            shelf = _CATCH_UP_SHELVES[(start + step) % count]
            fresh = [row for row in slice_for(shelf["key"]) if str(row.get("youtube_video_id") or "") not in used]
            if len(fresh) >= 3:
                chosen = fresh[:CATCH_UP_SIZE]
                used.update(str(row.get("youtube_video_id") or "") for row in chosen)
                plan[current] = {"shelf": shelf, "videos": chosen}
                break
        if current == 0:
            chosen_ids = {str(row.get("youtube_video_id") or "") for row in plan.get(0, {}).get("videos", [])}
            head = [row for row in pool if str(row.get("youtube_video_id") or "") not in chosen_ids]
            used.update(str(row.get("youtube_video_id") or "") for row in head[:_CATCH_UP_SUBSCRIPTION_HEAD])
    return plan


def _catch_up_section(pool: list[dict[str, Any]], seen: set[str], user_key: str, seed: int, page: int) -> dict[str, Any] | None:
    """This page's catch-up shelf, or None when its plan has fewer than three new videos."""
    entry = _catch_up_plan(pool, user_key, seed, page).get(page)
    if not entry:
        return None
    shelf = entry["shelf"]
    videos = _dedupe_section_videos(entry["videos"], seen, CATCH_UP_SIZE, spread_channels=False)
    if len(videos) < 3:
        return None
    return {
        "id": f'{shelf["id"]}-{page}',
        "eyebrow": shelf["eyebrow"],
        "title": shelf["title"],
        "videos": videos,
        "layout": shelf["layout"],
        "kind": "catch-up",
        "view_all": shelf.get("view_all", ""),
    }


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
    view_all: str = "",
) -> str:
    if layout == "shorts":
        return _shorts_shelf(videos, section_id)
    if not videos:
        return ""
    # An eyebrow above a heading that says the same thing is noise. Catch-up is the one
    # exception: its eyebrow is a signal, the light that says this shelf is unfinished.
    eyebrow = eyebrow if kind == "catch-up" else ""
    kind_attr = f' data-section-kind="{escape(kind)}"' if kind else ""
    if layout == "featured":
        return _shelf_html(section_id, title, videos[:12], settings, eyebrow=eyebrow, kind=kind, view_all=view_all)
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
          {_view_all_link(view_all, title)}
        </div>
        {body}
      </section>
    '''


HOME_PAGE_SIZE = 30
# How many recommendations open Home before the shelves. Three rows at four across, so
# Continue watching is one scroll away instead of sitting under a wall of thirty cards.
HOME_LEAD_SIZE = 12


def _home_page_lane(page: int) -> str:
    """The lane a deeper Home page (1, 2, ...) reads; see _compose_home_feed_sections."""
    return {1: "recommended", 2: "recent", 3: "long-form", 0: "recommended"}[page % 4]


def _home_lane_offset(page: int, lane: str) -> int:
    """Where page `page` starts in `lane`: each lane is read contiguously across pages.

    Page 0 shows recommended ranks 0-59. A deeper page continues its own lane from where that
    lane's previous page stopped, so no rank is skipped (pages 2-3 used to advance the
    recommended offset too, hiding ranks 90-149, 210-269, ...).
    """
    earlier = sum(1 for q in range(1, page) if _home_page_lane(q) == lane)
    start = HOME_PAGE_SIZE * 2 if lane == "recommended" else 0
    return start + earlier * HOME_PAGE_SIZE


def _without_muted_creators(sections: list[dict[str, Any]], user_key: str) -> list[dict[str, Any]]:
    """Muting ("Don't recommend channel") removes a creator from every Home shelf.

    The recommendation lanes exclude muted creators in the ranker, but the catch-up and
    subscription shelves on Home are built elsewhere and brought them back on reload. The
    Subscriptions page itself is untouched: muting means "don't recommend", not unsubscribe.
    """
    try:
        muted = {str(c).strip().lower() for c in (db.get_feed_preferences(user_key).get("muted_channels") or []) if str(c).strip()}
    except Exception:  # noqa: BLE001 - a page without the filter beats no page
        return sections
    if not muted:
        return sections
    out = []
    for section in sections:
        videos = section.get("videos")
        if isinstance(videos, list):
            kept = [v for v in videos if not (isinstance(v, dict) and db._recommendation_channel_key(v) in muted)]
            if not kept and videos:
                continue
            section = {**section, "videos": kept}
        out.append(section)
    return out


def _home_feed_sections(page: int, feed_filter: str, settings: Any, user_key: str = "local") -> list[dict[str, Any]]:
    sections = _without_hidden_shelves(_without_muted_creators(_compose_home_feed_sections(page, feed_filter, settings, user_key), user_key), user_key)
    # The v2 recommendation slots actually shown are logged to feed_impressions (on a
    # background thread, batched) so a later evaluation has exposure data. Never fatal.
    try:
        from . import ranker

        ranker.log_impressions(ranker.impression_records(sections, user_key, max(0, int(page or 0))))
    except Exception as exc:  # noqa: BLE001 - logging must not fail a page
        logger.info("feed impressions skipped: %s", exc)
    return sections


def _compose_home_feed_sections(page: int, feed_filter: str, settings: Any, user_key: str = "local") -> list[dict[str, Any]]:
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
        shorts = _dedupe_section_videos(db.list_ranked_short_videos(14, user_key), seen, 14) if show_shorts else []

        # Recommendations are recommendations. Subscription uploads used to be woven into
        # this shelf every few slots, which meant the same handful of followed creators
        # filled the top of the page and genuinely recommended videos were pushed off it.
        # They now have their own shelf below and their own page, so this run is ranked
        # purely on merit — topics, watch time, and everything else the scorer knows.
        recommended = _without_shorts(db.list_recommended_videos(HOME_PAGE_SIZE + 24, 0, "recommended", user_key))
        top_videos = _dedupe_section_videos(recommended, seen, HOME_PAGE_SIZE)
        lead_videos, lead_overflow = top_videos[:HOME_LEAD_SIZE], top_videos[HOME_LEAD_SIZE:]
        if lead_videos:
            sections.append({"id": "recommended", "eyebrow": "For you", "title": "Recommended for you", "videos": lead_videos, "layout": "grid", "kind": "recommended"})

        # Reserved before the remaining shelves so a catch-up variant is never
        # starved of videos, then placed further down the page below.
        catch_up = _catch_up_section(subscription_pool, seen, user_key, seed, page)

        continue_videos = _dedupe_section_videos(_without_shorts(db.list_recommended_videos(8, 0, "continue", user_key)), seen, 8, spread_channels=False)
        if continue_videos:
            sections.append({"id": "continue-watching", "eyebrow": "Resume", "title": "Continue watching", "videos": continue_videos, "layout": "featured", "kind": "continue", "view_all": "/?filter=continue"})
        sub_videos = _dedupe_section_videos(subscription_pool, seen, 12, spread_channels=False)
        if sub_videos:
            sections.append({"id": "new-subscriptions", "eyebrow": "Subscriptions", "title": "New from your subscriptions", "videos": sub_videos, "layout": "featured", "kind": "subscription", "view_all": "/subscriptions"})
        if shorts:
            sections.append({"id": "shorts", "eyebrow": "Shorts", "title": "Shorts", "videos": shorts, "layout": "shorts", "kind": "shorts"})
        # Ranks 30-59 of the same lane, so page 0 shows ranks 0-59 of one ranking. (It used to
        # read the "all" lane here, which skipped recommended ranks 30-59 for good and let
        # subscribed channels into a recommendation shelf.)
        more = lead_overflow + _dedupe_section_videos(
            _without_shorts(db.list_recommended_videos(HOME_PAGE_SIZE, HOME_PAGE_SIZE, "recommended", user_key)),
            seen,
            HOME_PAGE_SIZE,
        )
        if more:
            sections.append({"id": "recommended-more", "eyebrow": "Keep going", "title": "More recommendations", "videos": more, "layout": "grid", "kind": "recommended"})
        if catch_up:
            # Anywhere but the top: the first slot belongs to recommendations.
            sections.insert(min(len(sections), 1 + (seed % 3)), catch_up)
        return sections

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

    offset = _home_lane_offset(page, lane)
    # Ranker v2 serves the recent and long-form pages as filtered recommendation lanes; the
    # plain "recent" / "long-form" filter chips keep the classic ranker.
    served_lane = {"recent": "recommended-recent", "long-form": "recommended-long"}.get(lane, lane)
    videos = db.list_recommended_videos(HOME_PAGE_SIZE, offset, served_lane, user_key)
    if len(videos) < 8 and lane != "all":
        # A narrow lane runs dry long before the library does. Top it up rather than
        # returning an empty page, which would stop infinite scroll for good.
        videos = videos + db.list_recommended_videos(HOME_PAGE_SIZE, offset, "recommended", user_key)
        title, eyebrow = "More recommendations", "Keep going"
        section_id = f"more-{page}"

    # Deeper pages are recommendations too — subscriptions reach the feed through their
    # own shelf and the catch-up shelf below, not by being injected into every grid.
    videos = _dedupe_section_videos(_without_live(_without_shorts(videos)), seen, HOME_PAGE_SIZE)
    if videos:
        sections.append({"id": section_id, "eyebrow": eyebrow, "title": title, "videos": videos, "layout": "grid", "kind": "recommended"})

    # A catch-up shelf turns up every few pages, above or below that page's grid,
    # so it reads as part of the feed instead of a fixture pinned to the top.
    if _is_catch_up_page(page, seed):
        catch_up = _catch_up_section(subscription_pool, seen, user_key, seed, page)
        if catch_up:
            sections.insert(0 if (page + seed) % 2 == 0 else len(sections), catch_up)

    # Shorts keep reappearing as you scroll, the same way they do on the Shorts row.
    if show_shorts and sections and page % 3 == 2:
        shorts = _dedupe_section_videos(db.list_ranked_short_videos(14, user_key, offset=14 * (page // 3 + 1)), seen, 14)
        if shorts:
            sections.append({"id": f"shorts-{page}", "eyebrow": "Shorts", "title": "Shorts", "videos": shorts, "layout": "shorts", "kind": "shorts"})
    return sections


def _setup_checklist(user_key: str = "local") -> str:
    """The first-run card on Home. Every row completes from a real read, none from a click.

    It shows until there is both a subscription and some history to rank from, so a person
    who has just added their first channel can finish the list, and an established install
    never sees it. Dismissal is stored in the user's profile. Home must render regardless,
    so a failed read just leaves the card out.
    """
    try:
        preferences = db.get_feed_preferences(user_key)
        if preferences.get("setup_dismissed"):
            return ""
        subscriptions = len(db.list_subscriptions(250, user_key=user_key))
        history = len(db.list_history_videos(1, user_key))
        imported = db.latest_takeout_import(user_key)
        played = any(source_health_snapshot(source["id"]).get("last_ok") is not None for source in _configured_playback_sources(user_key))
    except Exception as exc:
        logger.warning("setup checklist read failed: %s", exc)
        return ""
    if subscriptions and (history or imported):
        return ""
    integrations = [name for key, name in (("sponsorblock_enabled", "SponsorBlock"), ("dearrow_enabled", "DeArrow"), ("ryd_enabled", "Dislike estimates")) if preferences.get(key)]
    subscription_note = f'{subscriptions}{"+" if subscriptions == 250 else ""} channel{"" if subscriptions == 1 else "s"} followed' if subscriptions else "Search for a channel and press Subscribe."
    rows = [
        (bool(imported), "Import Takeout", "Imported." if imported else "Start from your YouTube history.", 'data-open-settings-dialog data-settings-target="import"', ""),
        (bool(subscriptions), "Add a subscription", subscription_note, "", "/search"),
        (played, "Check playback health", "A source has played since this server started." if played else "Test that a source can play video.", 'data-open-settings-dialog data-settings-target="playback"', ""),
        (bool(integrations), "Optional integrations", ", ".join(integrations) + " on." if integrations else "SponsorBlock, DeArrow and dislike estimates are off.", 'data-open-settings-dialog data-settings-target="integrations"', ""),
    ]
    done = sum(1 for row in rows if row[0])
    items = []
    for complete, title, note, attrs, href in rows:
        inner = (
            f'<span class="setup-mark" aria-hidden="true">{_icon("check")}</span>'
            f'<span class="setup-copy"><strong>{escape(title)}</strong><small>{escape(note)}</small></span>'
            f'{_icon("chevron-right")}<span class="sr-only">{"Done" if complete else "Not done yet"}</span>'
        )
        target = f'<a class="setup-row" href="{escape(href, quote=True)}">{inner}</a>' if href else f'<button class="setup-row" type="button" {attrs}>{inner}</button>'
        items.append(f'<li class="{"is-done" if complete else ""}">{target}</li>')
    return (
        '<section class="setup-card" data-setup-card aria-labelledby="setup-title">'
        f'<div class="setup-head"><h2 id="setup-title">Set up OpenFeed</h2><span class="page-meta">{done} of {len(rows)}</span>'
        '<button class="btn btn-sm btn-ghost" type="button" data-setup-dismiss>Dismiss</button></div>'
        f'<ul class="setup-list">{"".join(items)}</ul></section>'
    )


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
            section.get("view_all", ""),
        )
        for section in _home_feed_sections(page, feed_filter, settings, user_key)
    )


def _home_empty_state(feed_filter: str) -> str:
    """What Home shows when a filter, or the whole feed, has nothing to list."""
    clear = '<button class="btn btn-sm" type="button" data-clear-filter>Clear filter</button>'
    if feed_filter == "continue":
        return _empty_state("history", "Nothing in progress", "Videos you start and leave unfinished appear here.", clear)
    if feed_filter != "all":
        return _empty_state("tune", "No videos match this filter", "Clear it to see everything, or pick another filter.", clear)
    return _empty_state("home", "No recommendations yet", "Videos appear here after the first sync. Subscribe to a channel or import your YouTube data from Settings.")


def _bundle(directory: Path, suffix: str) -> str:
    """The files in one asset directory, joined in filename order.

    The numeric prefixes are the cascade order, so a rule's file is part of its meaning.
    Keyed on mtime and size rather than cached outright: uvicorn's reloader only watches
    .py files, and a stylesheet edit has to show up without a restart.
    """
    signature = tuple(
        (path.name, stat.st_mtime_ns, stat.st_size)
        for path in sorted(directory.glob(f"*{suffix}"))
        for stat in (path.stat(),)
    )
    return _read_bundle(directory, signature)


@lru_cache(maxsize=8)
def _read_bundle(directory: Path, signature: tuple[tuple[str, int, int], ...]) -> str:
    return "".join((directory / name).read_text(encoding="utf-8") for name, _, _ in signature)


def _home_styles() -> str:
    """The app stylesheet: assets/css/*.css, concatenated. See DESIGN.md for the file map."""
    return _bundle(_ASSET_DIR / "css", ".css")



def _home_script() -> str:
    # The two thumbs come from the server's own icon table rather than being spelled out
    # again here. A hand-copied duplicate had already drifted from it, which is exactly
    # how the like and dislike buttons end up not matching.
    return _bundle(_ASSET_DIR / "js", ".js") + f'''
      const LIKE_ICON = '{_icon("like")}';
      const DISLIKE_ICON = '{_icon("dislike")}';
    ''' + '''
      const PLUS_ICON = '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14"/><path d="M5 12h14"/></svg>';
      const CHECK_ICON = '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="m5 12 4 4L19 6"/></svg>';
      const COLLECTIONS_ICON = '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="4" width="7" height="7" rx="2"/><rect x="14" y="4" width="7" height="7" rx="2"/><rect x="3" y="13" width="7" height="7" rx="2"/><rect x="14" y="13" width="7" height="7" rx="2"/></svg>';
      const CHEVRON_ICON = '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg>';
      /* Short names match the server-rendered trigger label; the longer ones are what the
         toast says, where there is room for a full sentence. */
      const NOTIFY_LABELS = { all: 'All', personalized: 'Personalized', none: 'None' };
      const NOTIFY_TOASTS = { all: 'All notifications', personalized: 'Personalized notifications', none: 'Notifications off' };
      const NOTIFY_ICONS = {
        all: '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M10.268 21a2 2 0 0 0 3.464 0"/><path d="M22 8c0-2.3-.8-4.3-2-6"/><path d="M3.262 15.326A1 1 0 0 0 4 17h16a1 1 0 0 0 .74-1.673C19.41 13.956 18 12.499 18 8A6 6 0 0 0 6 8c0 4.499-1.411 5.956-2.738 7.326"/><path d="M4 2C2.8 3.7 2 5.7 2 8"/></svg>',
        personalized: '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M10.268 21a2 2 0 0 0 3.464 0"/><path d="M3.262 15.326A1 1 0 0 0 4 17h16a1 1 0 0 0 .74-1.673C19.41 13.956 18 12.499 18 8A6 6 0 0 0 6 8c0 4.499-1.411 5.956-2.738 7.326"/></svg>',
        none: '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M10.268 21a2 2 0 0 0 3.464 0"/><path d="M17 17H4a1 1 0 0 1-.74-1.673C4.59 13.956 6 12.499 6 8a6 6 0 0 1 .258-1.742"/><path d="m2 2 20 20"/><path d="M8.668 3.01A6 6 0 0 1 18 8c0 2.687.77 4.653 1.707 6.05"/></svg>'
      };
      const dialog = document.getElementById('feed-dialog');
      const settingsDialog = document.getElementById('settings-dialog');
      const feedContainer = document.querySelector('[data-feed-container]');
      const loading = document.querySelector('[data-feed-loading]');
      const retryBox = document.querySelector('[data-feed-retry]');
      const sentinel = document.querySelector('[data-feed-sentinel]');
      let feedLoading = false;
      let feedHasMore = true;
      /* Bumped whenever a reset/filter request starts. A response from an older
         generation is dropped instead of overwriting what the user just chose. */
      let feedGeneration = 0;
      let feedAbort = null;
      let feedObserver = null;
      let descriptionCloseTimer = 0;

      function safeCss(value) {
        if (window.CSS && typeof CSS.escape === 'function') return CSS.escape(value);
        return String(value).replace(/[^a-zA-Z0-9_-]/g, '\\$&');
      }

      /* The page's short form of window.ofToast. Any other ofToast option can sit beside
         label and onClick. */
      function showToast(message, { label, onClick, ...options } = {}) {
        window.ofToast(message, { ...options, action: onClick, actionLabel: label });
      }

      /* A failure toast names the cause, so the two ways a request fails read differently. */
      function failureCause(error) {
        const text = String(error?.message || '');
        return text.startsWith('HTTP ') ? `the server answered ${text}` : 'the server could not be reached';
      }

      async function savePreferences(patch) {
        const response = await fetch('/api/feed/preferences', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(patch)
        });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return response.json();
      }

      /* Saving, Saved or Not saved, next to the control that changed. Saved clears itself. */
      function rowState(control, text, kind = '') {
        let state = control.parentElement.querySelector(':scope > .set-state');
        if (!state) {
          state = document.createElement('span');
          state.className = 'set-state';
          state.setAttribute('role', 'status');
          control.before(state);
        }
        window.clearTimeout(state.clearTimer);
        state.textContent = text;
        state.dataset.kind = kind;
        if (kind === 'saved') state.clearTimer = window.setTimeout(() => state.remove(), 2000);
      }

      async function saveControl(control, patch, savedMessage, failedLabel, revert) {
        const hadFocus = document.activeElement === control;
        control.disabled = true;
        rowState(control, 'Saving');
        try {
          await savePreferences(patch);
          rowState(control, 'Saved', 'saved');
          showToast(savedMessage, { kind: 'success' });
          return true;
        } catch (error) {
          revert?.();
          rowState(control, 'Not saved', 'failed');
          showToast(`Could not save ${failedLabel}: ${failureCause(error)}`, { kind: 'error' });
          return false;
        } finally {
          control.disabled = false;
          if (hadFocus) control.focus({ preventScroll: true });
        }
      }

      /* A switch that saves on change. preference maps the switch to the stored field. */
      function bindPreferenceSwitch(selector, preference, messages, failedLabel) {
        document.querySelector(selector)?.addEventListener('change', (event) => {
          const input = event.currentTarget;
          const on = input.checked;
          saveControl(input, preference(on), on ? messages[0] : messages[1], failedLabel, () => { input.checked = !on; });
        });
      }

      /* Raw output and other text behind a Copy. navigator.clipboard is missing on plain
         http, which is how a self-hosted install is often reached, so fall back to a
         selection and execCommand. */
      async function copyText(text, node) {
        try {
          await navigator.clipboard.writeText(text);
          return true;
        } catch (error) {
          const range = document.createRange();
          range.selectNodeContents(node);
          const selection = window.getSelection();
          selection.removeAllRanges();
          selection.addRange(range);
          try { return document.execCommand('copy'); } catch (copyError) { return false; }
        }
      }

      document.addEventListener('click', async (event) => {
        const button = event.target.closest('[data-copy-raw]');
        if (!button) return;
        const pre = button.parentElement.querySelector('pre');
        if (!pre) return;
        if (await copyText(pre.textContent, pre)) showToast('Copied');
        else showToast('Could not copy. The text is selected, so copy it with your keyboard.', { kind: 'error' });
      });

      /* The hop walk, shared by the Playback and Status panes. hops is the whole chain:
         the walk stops at the first break, so anything past it was skipped, not passed. */
      function hopBadge(text, tone) {
        const badge = document.createElement('span');
        badge.className = `hop-badge is-${tone}`;
        badge.textContent = text;
        return badge;
      }

      function hopTable(columns, rows) {
        const frame = document.createElement('div');
        frame.className = 'hop-frame';
        const table = document.createElement('table');
        table.className = 'hop-table';
        const head = document.createElement('tr');
        columns.forEach(([label, numeric]) => {
          const cell = document.createElement('th');
          cell.scope = 'col';
          cell.textContent = label;
          if (numeric) cell.className = 'hop-num';
          head.appendChild(cell);
        });
        const thead = document.createElement('thead');
        thead.appendChild(head);
        const tbody = document.createElement('tbody');
        rows.forEach((row) => tbody.appendChild(row));
        table.append(thead, tbody);
        frame.appendChild(table);
        return frame;
      }

      function hopRow(name, note, badge, latency, rowClass = '') {
        const row = document.createElement('tr');
        row.className = rowClass;
        const title = document.createElement('td');
        const strong = document.createElement('strong');
        strong.textContent = name;
        title.appendChild(strong);
        if (note) {
          const small = document.createElement('small');
          small.textContent = note;
          title.appendChild(small);
        }
        const result = document.createElement('td');
        result.appendChild(badge);
        const time = document.createElement('td');
        time.className = 'hop-num';
        time.textContent = Number.isFinite(latency) ? `${latency} ms` : '-';
        row.append(title, result, time);
        return row;
      }

      function rawOutput(label, text) {
        const details = document.createElement('details');
        details.className = 'raw-output';
        const summary = document.createElement('summary');
        summary.textContent = label;
        const body = document.createElement('div');
        body.className = 'raw-output-body';
        const copy = document.createElement('button');
        copy.type = 'button';
        copy.className = 'btn btn-sm';
        copy.dataset.copyRaw = '';
        copy.textContent = 'Copy';
        const pre = document.createElement('pre');
        pre.textContent = text;
        body.append(copy, pre);
        details.append(summary, body);
        return details;
      }

      function renderDiagnosis(box, diagnosis, hops = []) {
        box.replaceChildren();
        box.hidden = false;
        const title = document.createElement('h4');
        title.textContent = diagnosis.headline || '';
        const body = document.createElement('p');
        body.textContent = diagnosis.explanation || '';
        box.append(title, body);
        const steps = diagnosis.steps || [];
        const walked = new Map(steps.map((hop) => [hop.name, hop]));
        const chain = [...hops, ...steps.map((hop) => hop.name).filter((name) => !hops.includes(name))];
        let broken = false;
        const rows = chain.map((name) => {
          const hop = walked.get(name);
          if (!hop) return hopRow(name, '', hopBadge('Skipped', 'off'), NaN, 'is-skipped');
          if (hop.ok) return hopRow(name, hop.detail, hopBadge('OK', 'ok'), hop.latency_ms);
          const first = !broken;
          broken = true;
          return hopRow(name, [first ? 'First broken hop' : '', hop.detail].filter(Boolean).join(': '), hopBadge('Failed', 'bad'), hop.latency_ms, 'is-broken');
        });
        if (rows.length) box.appendChild(hopTable([['Hop'], ['Result'], ['Latency', true]], rows));
        if ((diagnosis.remedies || []).length) {
          const remedies = document.createElement('ul');
          diagnosis.remedies.forEach((item) => {
            const li = document.createElement('li');
            li.textContent = item;
            remedies.appendChild(li);
          });
          box.appendChild(remedies);
        }
        box.appendChild(rawOutput('Raw diagnosis output', JSON.stringify(diagnosis, null, 2)));
      }

      function updateReactionButtons(videoId, reaction) {
        document.querySelectorAll(`[data-feedback][data-video-id="${safeCss(videoId)}"]`).forEach((button) => {
          const eventType = button.dataset.feedback;
          if (eventType !== 'like' && eventType !== 'dislike') return;
          const selected = reaction === eventType;
          const wasSelected = button.classList.contains('is-selected');
          button.classList.toggle('is-selected', selected);
          button.setAttribute('aria-pressed', selected ? 'true' : 'false');
          /* The name follows the state; the count note is YouTube's figure and stays put. */
          if (button.hasAttribute('data-label-note')) {
            const word = eventType === 'like' ? (selected ? 'Liked' : 'Like') : (selected ? 'Disliked' : 'Dislike');
            button.setAttribute('aria-label', `${word}${button.dataset.labelNote || ''}`);
            button.dataset.tip = word;
          }
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
        if (!menu || window.matchMedia('(max-width: 600px)').matches) return;
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
        if (menu.closest('.shelf-track, .shorts-row')) {
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
        if (!feedContainer) return;
        feedContainer.dataset.feedFilter = filter;
        document.querySelector('.creator-strip')?.toggleAttribute('hidden', filter !== 'all');
        /* The filter is part of the address, so a reload keeps it and a link can open it. */
        const params = new URLSearchParams(window.location.search);
        if (filter === 'all') params.delete('filter');
        else params.set('filter', filter);
        const query = params.toString();
        window.history.replaceState(window.history.state, '', query ? `/?${query}` : '/');
      }

      /* A new filter replaces the page, so the old cards go at once and the skeleton
         holds the grid until the answer arrives. */
      function showFeedSkeleton() {
        const cards = loading?.querySelector('.video-grid')?.innerHTML;
        if (!feedContainer || !cards) return;
        feedContainer.setAttribute('aria-busy', 'true');
        feedContainer.innerHTML = `<div class="section-heading"><span class="skeleton skeleton-heading"></span></div><div class="video-grid">${cards.repeat(3)}</div>`;
        if (window.scrollY > 0) window.scrollTo({ top: 0 });
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
        if (!feedContainer || (!feedHasMore && !options.reset)) return;
        /* One request at a time. Swapping the page for the skeleton pulls the sentinel
           into view, so the observer must wait for the reset rather than start a second
           load of page 0 that would supersede it and append to the skeleton. */
        if (feedLoading && !options.reset) return;
        /* A new generation per request; a reset cancels whatever is still in flight and
           anything that slips through is dropped below. */
        feedAbort?.abort();
        const controller = new AbortController();
        feedAbort = controller;
        const generation = ++feedGeneration;
        const filter = options.filter || currentFilter();
        const page = options.reset ? 0 : Number(feedContainer.dataset.nextPage || '1');
        feedLoading = true;
        if (options.reset) showFeedSkeleton();
        if (loading) loading.hidden = Boolean(options.reset);
        if (retryBox) retryBox.hidden = true;
        let loaded = false;
        try {
          const response = await fetch(`/api/home/sections?page=${page}&filter=${encodeURIComponent(filter)}`, { signal: controller.signal });
          if (generation !== feedGeneration) return;
          if (!response.ok) throw new Error('section load failed');
          const data = await response.json();
          if (generation !== feedGeneration) return;
          let html = options.reset ? data.html : dedupeIncomingHtml(data.html || '');
          if (options.reset) {
            feedContainer.innerHTML = html || data.empty_html || '';
          } else if (html) {
            feedContainer.insertAdjacentHTML('beforeend', html);
          }
          feedContainer.dataset.nextPage = String(data.next_page || page + 1);
          feedContainer.dataset.feedFilter = filter;
          /* has_more was computed server-side before the client dropped duplicate
             cards. A page that dedupes to nothing therefore must not end the feed:
             keep scrolling and let the server terminate it with an empty page. */
          feedHasMore = Boolean(data.has_more);
          loaded = true;
        } catch (error) {
          if (generation === feedGeneration) {
            if (retryBox) retryBox.hidden = false;
            if (options.reset && feedContainer) {
              if (retryBox) retryBox.hidden = true;
              feedContainer.innerHTML = '<div class="empty-state"><h2>Could not load the feed</h2><p>The server did not answer this request.</p><div class="empty-state-action"><button class="btn btn-sm" type="button" data-retry-feed>Retry</button></div></div>';
            }
          }
        } finally {
          if (generation === feedGeneration) {
            feedLoading = false;
            feedContainer?.removeAttribute('aria-busy');
            window.ofHideLoader(loading);
            /* The sentinel may never have left the view: a short page does not push it
               out, and an observer only reports changes. Observing again asks once more. */
            if (loaded && feedHasMore && sentinel) {
              feedObserver?.unobserve(sentinel);
              feedObserver?.observe(sentinel);
            }
          }
        }
      }

      /* Pull to refresh (assets/js/search-home.js) starts the feed over from page 0. */
      window.ofReloadFeed = () => {
        feedHasMore = true;
        return loadFeedPage({ reset: true });
      };

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

        document.querySelectorAll(`[data-subscription-card]${match}`).forEach((card) => {
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
          const totalHead = document.querySelector('[data-subscription-total]');
          if (totalHead && document.querySelector('[data-subscription-card]')) {
            const next = Math.max(0, Number(totalHead.dataset.subscriptionTotal || '0') - 1);
            totalHead.dataset.subscriptionTotal = String(next);
            const meta = totalHead.querySelector('.page-meta');
            if (meta) meta.textContent = `${next} channel${next === 1 ? '' : 's'}`;
          }
          const timeline = document.querySelector('.timeline[data-section-kind="subscription"]');
          if (timeline && !timeline.querySelector('[data-video-card]')) {
            timeline.outerHTML = '<div class="empty-state"><h2>No videos from your subscriptions yet</h2><p>Uploads from the channels you follow appear here after the next sync.</p><div class="empty-state-action"><button class="btn" type="button" data-feed-refresh>Sync now</button></div></div>';
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
          window.ofOpenSettings?.(openSettingsDialog.dataset.settingsTarget, openSettingsDialog);
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
          /* nextPage 0 means a filter change is what failed, so this replaces the error. */
          await loadFeedPage({ reset: feedContainer?.dataset.nextPage === '0' });
          return;
        }

        const deleteCollection = event.target.closest('[data-delete-collection]');
        if (deleteCollection) {
          event.preventDefault();
          const collectionId = deleteCollection.dataset.deleteCollection;
          if (!collectionId) return;
          closeMenus();
          /* No confirm: the toast's Undo restores it (library-tools.js). */
          await window.ofDeleteWithUndo(deleteCollection, {
            url: `/api/subscription-collections/${encodeURIComponent(collectionId)}`,
            card: '[data-collection-card]', message: 'Collection deleted', failure: 'Could not delete collection',
          });
          return;
        }

        const deletePlaylist = event.target.closest('[data-delete-playlist]');
        if (deletePlaylist) {
          event.preventDefault();
          const playlistId = deletePlaylist.dataset.deletePlaylist;
          if (!playlistId) return;
          closeMenus();
          /* No confirm: the toast's Undo restores it (library-tools.js). */
          await window.ofDeleteWithUndo(deletePlaylist, {
            url: `/api/playlists/${encodeURIComponent(playlistId)}`,
            card: '[data-playlist-card]', message: 'Playlist deleted', failure: 'Could not delete playlist',
          });
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
            const memberCount = document.querySelector('[data-member-count]');
            if (memberCount) memberCount.textContent = String(document.querySelectorAll('[data-member-card]').length);
            showToast('Removed from collection');
            if (!document.querySelector('[data-member-card]')) window.location.reload();
          } catch (error) {
            showToast('Could not remove channel', { kind: 'error' });
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
          openPlaylistEditor(editPlaylist.dataset.editPlaylist, editPlaylist.dataset.playlistName || '', editPlaylist.dataset.playlistDescription || '', editPlaylist.dataset.playlistVisibility || '');
          return;
        }

        const saveToPlaylist = event.target.closest('[data-save-to-playlist]');
        if (saveToPlaylist) {
          event.preventDefault();
          closeMenus();
          window.ofOpenSavePicker?.(saveToPlaylist.dataset.saveToPlaylist, saveToPlaylist.dataset.videoTitle || '');
          return;
        }

        const why = event.target.closest('[data-why-video]');
        if (why) {
          /* The reason is already on the row; this repeats it in full where it cannot clip. */
          event.preventDefault();
          closeMenus();
          showToast(`Why this video: ${why.dataset.whyVideo}`, { duration: 8000 });
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
          if (subscribeButton.closest('.menu-panel')) closeMenus();
          if (unsubscribe && !await window.ofConfirm({ title: `Unsubscribe from ${title || 'this channel'}?`, confirmLabel: 'Unsubscribe', danger: true })) return;
          subscribeButton.disabled = true;
          try {
            const response = await fetch('/api/subscriptions', {
              method: unsubscribe ? 'DELETE' : 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ feed_url: feedUrl, youtube_channel_id: channelId, title })
            });
            if (!response.ok) throw new Error('subscription change failed');
            subscribeButton.classList.toggle('subscribed', !unsubscribe);
            const label = subscribeButton.querySelector('.menu-label') || subscribeButton.querySelector('span');
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
                    const undo = await fetch('/api/subscriptions', {
                      method: 'POST',
                      headers: { 'Content-Type': 'application/json' },
                      body: JSON.stringify({ feed_url: feedUrl, youtube_channel_id: channelId, title })
                    });
                    if (!undo.ok) throw new Error('resubscribe failed');
                    window.location.reload();
                  } catch (error) {
                    showToast('Could not resubscribe', { kind: 'error' });
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
            showToast(unsubscribe ? 'Could not unsubscribe' : 'Could not subscribe', { kind: 'error' });
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

        /* The whole card opens and closes. Links and buttons keep their own click, and a
           drag leaves a selection, so reading and copying never collapse it. */
        const descriptionCard = event.target.closest('[data-description-box]');
        if (descriptionCard && !event.target.closest('a, button') && !String(window.getSelection())) {
          const toggle = descriptionCard.querySelector('[data-description-toggle]');
          if (!toggle || toggle.hidden) return;
          if (descriptionCard.classList.contains('is-collapsed')) { toggle.click(); return; }
          /* Closing waits out a double click, which is selecting a word. */
          window.clearTimeout(descriptionCloseTimer);
          if (event.detail > 1) return;
          descriptionCloseTimer = window.setTimeout(() => {
            if (!String(window.getSelection())) toggle.click();
          }, 260);
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
            showToast('Could not save that', { kind: 'error' });
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
               seeking without moving looks like nothing happened. A player that is already
               on screen stays put: chapters sit beside it, and on a phone it is pinned. */
            const stage = document.querySelector('.watch-stage') || player;
            if (stage.getBoundingClientRect().bottom < 120) stage.scrollIntoView({ behavior: 'smooth', block: 'start' });
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
              body: JSON.stringify({ youtube_video_id: videoId, event_type: eventType, payload: { source: 'ui_menu', shown_reason: card?.dataset.reason || card?.dataset.source || '', topics: card?.dataset.topics || '' } })
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
                    /* With the event id the press is taken back whole (search-home.js's
                       endpoint); undo_hide is the fallback for an older response. */
                    const undo = data.event?.id && data.previous
                      ? await fetch('/api/feedback/undo', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ youtube_video_id: videoId, event_id: data.event.id, previous: data.previous })
                      })
                      : await fetch('/api/feedback', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ youtube_video_id: videoId, event_type: 'undo_hide', payload: { source: 'undo_toast' } })
                      });
                    if (!undo.ok) throw new Error('undo failed');
                    cards.forEach((item) => item.hidden = false);
                    showToast('Restored');
                  } catch (error) {
                    showToast('Could not undo', { kind: 'error' });
                  }
                }
              });
            } else if (eventType === 'mark_watched') {
              window.ofMarkWatched(videoId);
              showToast('Marked watched');
            } else if (eventType === 'mark_unwatched') {
              window.ofMarkUnwatched(videoId);
              showToast('Marked unwatched');
            } else if ((eventType === 'more_like_this' || eventType === 'less_like_this') && window.ofFeedbackToast) {
              /* The toast with Undo lives in assets/js/search-home.js. */
              window.ofFeedbackToast(videoId, eventType, data);
            } else {
              showToast(eventType === 'clear_reaction' ? 'Reaction cleared' : 'Saved');
            }
          } catch (error) {
            showToast('Could not save', { kind: 'error' });
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
      let editingPlaylistId = null;

      function openPlaylistEditor(playlistId, name, description, visibility) {
        if (!playlistEditDialog || !playlistEditForm) return;
        editingPlaylistId = playlistId || null;
        playlistEditForm.querySelector('[name="name"]').value = name || '';
        playlistEditForm.querySelector('[name="description"]').value = description || '';
        const visibilityField = playlistEditForm.querySelector('[name="visibility"]');
        if (visibilityField) visibilityField.value = visibility || 'private';
        playlistEditForm.querySelector('[data-playlist-import-alt]')?.toggleAttribute('hidden', Boolean(playlistId));
        playlistEditDialog.querySelector('#playlist-edit-title').textContent = playlistId ? 'Edit playlist' : 'New playlist';
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
          description: String(data.get('description') || '').trim() || null,
          visibility: String(data.get('visibility') || '') || null
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
          showToast('Could not update collection', { kind: 'error' });
        } finally {
          button.disabled = false;
        }
      }

      channelCollectionsDialog?.addEventListener('close', () => {
        /* The rows behind the dialog show which collections each channel sits in. */
        if (membershipDirty && document.querySelector('[data-subscription-card]')) window.location.reload();
      });

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
          showToast('Could not create collection', { kind: 'error' });
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
        /* The slider in the markup carries data-tune-input; querying data-tune-range
           never matched anything, so the live "is-sliding" visuals never fired. */
        const range = tuneRoot.querySelector('[data-tune-input]');
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
            discovery_level: Number(formData.get('discovery_level') || 35) / 100,
            ranker_mode: String(formData.get('ranker_mode') || '')
          };
          if (preferenceStatus) preferenceStatus.textContent = 'Saving';
          try {
            await savePreferences(payload);
          } catch (error) {
            const cause = failureCause(error);
            if (preferenceStatus) preferenceStatus.textContent = `Not saved: ${cause}`;
            showToast(`Could not save feed controls: ${cause}`, { kind: 'error' });
            return;
          }
          if (preferenceStatus) preferenceStatus.textContent = '';
          commitFormDefaults(preferenceForm);
          if (dialog) dialog.close();
          showToast('Feed controls saved', { kind: 'success' });
          feedHasMore = true;
          if (feedContainer) feedContainer.dataset.nextPage = '0';
          await loadFeedPage({ reset: true, filter: currentFilter() });
        });
      }

      /* What was saved becomes what the form resets to. */
      function commitFormDefaults(form) {
        form.querySelectorAll('input, textarea').forEach((input) => {
          if (input.type === 'checkbox' || input.type === 'radio') input.defaultChecked = input.checked;
          else input.defaultValue = input.value;
        });
        form.querySelectorAll('select option').forEach((option) => { option.defaultSelected = option.selected; });
      }

      /* Closing with X, Escape or the back gesture drops unsaved edits, so the next open shows
         what is stored rather than edits that look saved. Mutes and hidden shelves save as
         they are pressed and are not form fields, so they stay. */
      if (preferenceForm && dialog) {
        dialog.addEventListener('close', () => {
          preferenceForm.reset();
          preferenceForm.querySelectorAll('input.fc-slider').forEach((input) => input.dispatchEvent(new Event('input', { bubbles: true })));
          preferenceForm.querySelectorAll('[data-topic-input]').forEach((input) => input.renderTopicChips?.());
          syncTuneVisuals();
          if (preferenceStatus) preferenceStatus.textContent = '';
        });
      }

      document.querySelector('[data-reset-feed-preferences]')?.addEventListener('click', async () => {
        if (!preferenceForm) return;
        if (!await window.ofConfirm({
          title: 'Reset feed controls?',
          body: 'Clears your topics, sets discovery back to 35%, prioritizes subscriptions and includes Shorts. Muted creators stay muted.',
          confirmLabel: 'Reset',
          danger: true,
        })) return;
        preferenceForm.querySelector('[name="preferred_topics"]').value = '';
        preferenceForm.querySelector('[name="muted_topics"]').value = '';
        preferenceForm.querySelector('[name="prioritize_subscriptions"]').checked = true;
        preferenceForm.querySelector('[name="exclude_shorts"]').checked = false;
        const rankerMode = preferenceForm.querySelector('[name="ranker_mode"]');
        if (rankerMode) rankerMode.value = '';
        const discovery = preferenceForm.querySelector('[name="discovery_level"]');
        discovery.value = 35;
        discovery.dispatchEvent(new Event('input', { bubbles: true }));
        preferenceForm.querySelectorAll('[data-topic-input]').forEach((input) => input.renderTopicChips?.());
        syncTuneVisuals();
        preferenceForm.requestSubmit();
      });

      if (sentinel && feedContainer && 'IntersectionObserver' in window) {
        feedObserver = new IntersectionObserver((entries) => {
          if (entries.some((entry) => entry.isIntersecting)) loadFeedPage();
        }, { rootMargin: '900px 0px' });
        feedObserver.observe(sentinel);
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
        /* Delayed mounts are tracked per slide so an eviction or a window resync can
           cancel them: otherwise a stale timer could mount a player the window just
           decided to evict. */
        const preloadTimers = new WeakMap();
        function cancelPreload(slide) {
          const pending = preloadTimers.get(slide);
          if (pending) { window.clearTimeout(pending); preloadTimers.delete(slide); }
        }
        function schedulePreload(slide, distance) {
          cancelPreload(slide);
          const timer = window.setTimeout(() => {
            preloadTimers.delete(slide);
            if (!slide.isConnected) return;
            /* The window may have moved while the timer was pending. */
            const list = slides();
            const pos = list.indexOf(slide);
            const anchor = Math.max(0, list.indexOf(activeSlide));
            if (pos < 0 || Math.abs(pos - anchor) > PRELOAD_AHEAD) { unmountPlayer(slide); return; }
            mountPlayer(slide, false);
          }, 400 * Math.max(1, Math.abs(distance)));
          preloadTimers.set(slide, timer);
        }

        function mountPlayer(slide, active) {
          const frame = slide.querySelector('[data-shorts-frame]');
          if (!frame) return;
          let iframe = frame.querySelector('iframe');
          if (!iframe) {
            const videoId = slide.dataset.videoId;
            iframe = document.createElement('iframe');
            /* Always autoplay=0, on screen or not. A frame that starts itself is already
               running by the time it can hear a pause, so a Short flicked past while it
               loaded kept going. The shell asks whether to play once it can listen. */
            iframe.src = `/watch/${encodeURIComponent(videoId)}/player?autoplay=0&short=1`;
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

        function tell(iframe, message) {
          const body = typeof message === 'string' ? { type: message } : message;
          try { iframe?.contentWindow?.postMessage(body, window.location.origin); } catch (error) { /* not ready yet */ }
        }

        function pausePlayer(slide) {
          const iframe = slide.querySelector('[data-shorts-frame] iframe');
          if (iframe) tell(iframe, 'openfeed:pause');
          slide.classList.remove('is-playing');
        }

        function unmountPlayer(slide) {
          cancelPreload(slide);
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
            cancelPreload(slide);
            if (distance === 0) return;
            /* Staggered so arriving at a Short does not fire three player loads at once.
               The immediate neighbour is worth loading now; the one after can wait. */
            if (distance === 1) mountPlayer(slide, false);
            else schedulePreload(slide, distance);
          });
        }

        function setActive(slide) {
          if (!slide || slide === activeSlide) return;
          if (activeSlide) pausePlayer(activeSlide);
          activeSlide = slide;
          mountPlayer(slide, true);
          syncWindow(slides().indexOf(slide));
          recordShortView(slide.dataset.videoId);
          paintMute();
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
          /* Arrow keys belong to whatever has focus: with a dialog or menu open they are
             navigation inside it, not paging through the Short behind the overlay. */
          if (document.querySelector('dialog[open]')) return;
          if (target instanceof Element && target.closest('details[open], [aria-expanded="true"]')) return;
          if (event.ctrlKey || event.metaKey || event.altKey) return;
          if (event.key === 'ArrowDown' || event.key === 'j') { event.preventDefault(); go(1); }
          else if (event.key === 'ArrowUp') { event.preventDefault(); go(-1); }
          else if (event.key === 'k' || (event.key === ' ' && !(target instanceof Element && target.closest('a, button:not([data-shorts-tap]), summary')))) {
            /* Space on a focused rail button presses that button; anywhere else it would
               scroll the deck to the next Short. k matches YouTube's play/pause key. */
            event.preventDefault();
            togglePlay();
          }
          else if (event.key === 'm') setMuted(!shortsMuted);
          else if (event.key === 'c' && activeSlide) activeSlide.querySelector('[data-shorts-comments]')?.click();
        });

        /* The player is an iframe, so its clock and its tap arrive as messages. */
        let shortsMuted = false;
        let mutedByHand = false;
        function setMuted(muted) {
          shortsMuted = muted;
          mutedByHand = muted;
          tell(activeSlide?.querySelector('[data-shorts-frame] iframe'), { type: 'openfeed:mute', muted });
          paintMute();
        }
        /* A toggle button keeps one name and reports its state through aria-pressed: "Mute",
           pressed while muted. A name that flipped as well would read "Unmute, pressed". */
        function paintMute() {
          stage.querySelectorAll('[data-shorts-mute]').forEach((button) => {
            button.setAttribute('aria-pressed', shortsMuted ? 'true' : 'false');
            button.setAttribute('aria-label', 'Mute');
            button.classList.toggle('is-selected', shortsMuted);
            button.querySelector('[data-mute-label]').textContent = shortsMuted ? 'Muted' : 'Sound';
          });
        }
        window.addEventListener('message', (event) => {
          if (event.origin !== window.location.origin) return;
          const data = event.data || {};
          const slide = slides().find((item) => item.querySelector('[data-shorts-frame] iframe')?.contentWindow === event.source);
          if (!slide) return;
          if (data.type === 'openfeed:clock') {
            const fill = slide.querySelector('[data-shorts-progress]');
            if (fill && data.duration) fill.style.transform = `scaleX(${Math.min(1, data.seconds / data.duration)})`;
            slide.classList.toggle('is-paused', Boolean(data.paused) && slide === activeSlide);
            /* Only the Short on screen runs. A pause sent before this frame was listening
               never arrived, and the clock is what shows it is still going. */
            if (slide !== activeSlide && !data.paused) tell(slide.querySelector('[data-shorts-frame] iframe'), 'openfeed:pause');
            if (slide === activeSlide && Boolean(data.muted) !== shortsMuted) {
              /* Each new Short starts unmuted, so a mute that was asked for is reapplied. A
                 mute the browser imposed on autoplay is only shown, so it can be undone. */
              if (mutedByHand) tell(slide.querySelector('[data-shorts-frame] iframe'), { type: 'openfeed:mute', muted: true });
              else { shortsMuted = Boolean(data.muted); paintMute(); }
            }
          } else if (data.type === 'openfeed:shell-ready') {
            tell(slide.querySelector('[data-shorts-frame] iframe'), slide === activeSlide ? 'openfeed:play' : 'openfeed:pause');
          } else if (data.type === 'openfeed:playback-failed') {
            slide.classList.add('is-failed');
          } else if (data.type === 'openfeed:playback-ready') {
            slide.classList.remove('is-failed');
          } else if (data.type === 'openfeed:opaque-player') {
            slide.classList.add('is-opaque');
          }
        });
        stage.addEventListener('click', (event) => {
          if (event.target.closest('[data-shorts-mute]')) { setMuted(!shortsMuted); return; }
          const tap = event.target.closest('[data-shorts-tap]');
          if (tap) tell(tap.closest('[data-shorts-slide]').querySelector('[data-shorts-frame] iframe'), 'openfeed:toggle');
        });
        function togglePlay() {
          if (!activeSlide) return;
          const iframe = activeSlide.querySelector('[data-shorts-frame] iframe');
          if (iframe && activeSlide.classList.contains('is-playing')) tell(iframe, 'openfeed:toggle');
          else activeSlide.querySelector('[data-shorts-play]')?.click();
        }

        // Append more Shorts as the feed nears its end.
        let lastFailure = 0;
        async function loadMore() {
          if (loading || exhausted) return;
          /* Back off after a failed page: retrying on every scroll event would hammer
             the server, but unlike "exhausted" a transient error stays recoverable. */
          if (Date.now() - lastFailure < 5000) return;
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
            lastFailure = Date.now();
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

        function showEnableComments(videoId) {
          panelBody.replaceChildren();
          const wrap = document.createElement('div');
          wrap.className = 'shorts-enable';
          const copy = document.createElement('p');
          /* Show comments is one setting for the whole app, so the button says that
             rather than implying it unlocks only this Short. */
          copy.textContent = 'Comments are off in Settings. Turning them on shows comments everywhere: on Shorts and on watch pages. You can switch them off again in Settings, Content.';
          const button = document.createElement('button');
          button.className = 'primary-action';
          button.type = 'button';
          button.textContent = 'Turn on comments everywhere';
          button.dataset.shortsEnableComments = '';
          button.addEventListener('click', async () => {
            button.disabled = true;
            try {
              /* A partial update: only show_comments changes. */
              const saved = await fetch('/api/feed/preferences', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ show_comments: true })
              });
              if (!saved.ok) throw new Error('could not enable comments');
              panel.dataset.commentsEnabled = '1';
              window.ofToast?.('Comments are on everywhere. Settings, Content switches them off.', { kind: 'success' });
              loadShortComments(videoId);
            } catch (error) {
              button.disabled = false;
              window.ofToast?.('Could not turn on comments. Try again.', { kind: 'error' });
            }
          });
          wrap.append(copy, button);
          panelBody.appendChild(wrap);
        }

        let commentsGeneration = 0;
        async function loadShortComments(videoId) {
          if (!panelBody || !videoId) return;
          const generation = ++commentsGeneration;
          const stale = () => generation !== commentsGeneration;
          /* Off in Settings: offer to turn them on without first asking the API for a 403. */
          if (panel.dataset.commentsEnabled === '0') { showEnableComments(videoId); return; }
          commentSkeleton(panelBody);
          try {
            const response = await fetch(`/api/comments/${encodeURIComponent(videoId)}`);
            if (stale()) return;
            /* Replies and votes find their video through this attribute, as they do on the
               watch page; without it every reply toggle in this panel asked for nothing. */
            panelBody.dataset.commentsFor = videoId;
            if (response.status === 403) { showEnableComments(videoId); return; }
            if (!response.ok) throw new Error(await commentFailure(response));
            if (stale()) return;
            const data = await response.json();
            if (stale()) return;
            const items = data.items || [];
            panelBody.replaceChildren();
            if (!items.length) {
              const empty = document.createElement('p');
              empty.className = 'comments-state';
              empty.textContent = 'No comments on this Short.';
              panelBody.appendChild(empty);
              return;
            }
            if (data.notice) {
              const notice = document.createElement('p');
              notice.className = 'comments-state';
              notice.dataset.commentsNotice = '';
              notice.textContent = data.notice;
              panelBody.appendChild(notice);
            }
            items.forEach((item) => panelBody.appendChild(renderComment(item)));
            resolveCommentAvatars(panelBody);
          } catch (error) {
            panelBody.replaceChildren();
            const failed = document.createElement('p');
            failed.className = 'comments-state';
            failed.textContent = error.message || 'Comments could not be loaded.';
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
          if (event.target.closest('[data-shorts-comments-close]')) closeShortComments();
        });
        function closeShortComments() {
          if (!panel) return;
          panel.hidden = true;
          viewport.classList.remove('comments-open');
        }
        document.addEventListener('keydown', (event) => {
          if (event.key === 'Escape' && panel && !panel.hidden) closeShortComments();
        });
        dragToClose(document.querySelector('[data-shorts-comments-handle]'), panel, closeShortComments);

        syncActive();
      })();

      /* ---- notifications ---- */
      const notificationsMenu = document.querySelector('[data-notifications]');
      let notificationsLoaded = false;

      function renderNotification(item) {
        const row = document.createElement('a');
        row.className = item.unread ? 'notification-row unread' : 'notification-row';
        row.href = `/watch/${encodeURIComponent(item.youtube_video_id)}`;

        const dot = document.createElement('span');
        dot.className = 'notification-dot';

        const copy = document.createElement('span');
        copy.className = 'notification-copy';
        /* Unread is not color-only: the group heading says it, and so does this prefix. */
        if (item.unread) {
          const flag = document.createElement('span');
          flag.className = 'sr-only';
          flag.textContent = 'New: ';
          copy.append(flag);
        }
        const what = document.createElement('strong');
        what.textContent = item.title;
        const when = document.createElement('small');
        when.textContent = [item.channel_title, item.published_text, item.duration_text].filter(Boolean).join(' \u00b7 ');
        copy.append(what, when);

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

        row.append(dot, copy, thumbWrap);
        return row;
      }

      function notificationGroup(label, items) {
        const group = document.createElement('section');
        const heading = document.createElement('h3');
        heading.className = 'notification-group';
        heading.textContent = label;
        group.append(heading, ...items.map(renderNotification));
        return group;
      }

      function setNotificationBadge(unread) {
        const badge = document.querySelector('[data-bell-badge]');
        if (!badge) return;
        badge.hidden = !unread;
        badge.textContent = unread > 9 ? '9+' : unread ? String(unread) : '';
      }

      async function loadNotifications() {
        const list = notificationsMenu?.querySelector('[data-notification-list]');
        const summary = notificationsMenu?.querySelector('[data-notification-summary]');
        const markRead = notificationsMenu?.querySelector('[data-notifications-read]');
        if (!list) return;
        try {
          const response = await fetch('/api/notifications');
          if (!response.ok) throw new Error('load failed');
          const data = await response.json();
          const items = data.items || [];
          const fresh = items.filter((item) => item.unread);
          const earlier = items.filter((item) => !item.unread);
          list.replaceChildren();
          if (!items.length) {
            list.innerHTML = '<div class="empty-state notification-empty"><h2>No notifications</h2><p>New uploads from the channels you subscribe to appear here.</p></div>';
          } else {
            if (fresh.length) list.appendChild(notificationGroup('New', fresh));
            if (earlier.length) list.appendChild(notificationGroup('Earlier', earlier));
          }
          if (summary) summary.textContent = data.unread ? `${data.unread} new` : 'From your subscriptions';
          if (markRead) markRead.hidden = !data.unread;
          /* The list only holds the latest rows, so the server's count is the real one. */
          setNotificationBadge(data.unread);
        } catch (error) {
          notificationsLoaded = false;
          list.innerHTML = '<div class="empty-state notification-empty"><h2>Could not load notifications</h2><p>The server did not answer this request.</p><div class="empty-state-action"><button class="btn btn-sm" type="button" data-notifications-retry>Retry</button></div></div>';
        }
      }

      notificationsMenu?.addEventListener('toggle', () => {
        if (notificationsMenu.open && !notificationsLoaded) {
          notificationsLoaded = true;
          loadNotifications();
        }
      });

      notificationsMenu?.addEventListener('click', async (event) => {
        if (event.target.closest('[data-notifications-retry]')) {
          notificationsLoaded = true;
          loadNotifications();
          return;
        }
        const markRead = event.target.closest('[data-notifications-read]');
        if (!markRead) return;
        markRead.disabled = true;
        try {
          const response = await fetch('/api/notifications/seen', { method: 'POST' });
          if (!response.ok) throw new Error('seen failed');
          await loadNotifications();
        } catch (error) {
          showToast('Could not mark notifications read', { kind: 'error' });
        } finally {
          markRead.disabled = false;
        }
      });

      /* ---- recommendation ranking mode ---- */
      document.querySelector('[data-setting-ranker]')?.addEventListener('change', (event) => {
        const select = event.currentTarget;
        const previous = select.dataset.saved ?? [...select.options].find((option) => option.defaultSelected)?.value ?? '';
        saveControl(select, { ranker_mode: select.value }, `Ranking: ${select.selectedOptions[0]?.textContent}`, 'ranking', () => { select.value = previous; })
          .then((saved) => { if (saved) select.dataset.saved = select.value; });
      });

      /* ---- settings switches ---- */
      bindPreferenceSwitch('[data-setting-shorts]', (on) => ({ exclude_shorts: !on }), ['Shorts are shown', 'Shorts are hidden'], 'Show Shorts');
      bindPreferenceSwitch('[data-setting-ryd]', (on) => ({ ryd_enabled: on }), ['Dislike estimates on. They fill in over the next background sync.', 'Dislike estimates off. Counts already fetched stay cached.'], 'Estimated dislike counts');
      bindPreferenceSwitch('[data-setting-comments]', (on) => ({ show_comments: on }), ['Comments will appear on watch pages', 'Comments are hidden'], 'Show comments');
      bindPreferenceSwitch('[data-setting-posts]', (on) => ({ show_posts_in_feeds: on }), ['Posts will appear in your feeds', 'Posts hidden from feeds'], 'Show posts in feeds');
      bindPreferenceSwitch('[data-setting-explore]', (on) => ({ explore_enabled: on }), ['Tuning on. Recommended now mixes both rankings.', 'Tuning off. Recommended uses your ranking alone.'], 'Help tune recommendations');

      /* ---- playback sources and fallback control ---- */
      (() => {
        const list = document.querySelector('[data-playback-sources]');
        if (!list) return;
        const editor = document.querySelector('[data-instance-editor]');
        const chips = document.querySelector('[data-instance-chips]');
        const status = document.querySelector('[data-playback-status]');
        const progress = document.querySelector('[data-playback-progress]');
        const progressFill = document.querySelector('[data-playback-progress-fill]');
        const resultsBox = document.querySelector('[data-playback-results]');
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
          return save({ disabled_sources: [...disabled] });
        }

        function persistOrder() {
          save({ source_order: currentSources.map((source) => source.id) }, { quiet: true });
        }

        function moveSource(from, to, keepFocus = '') {
          if (to < 0 || to >= currentSources.length) return;
          const [moved] = currentSources.splice(from, 1);
          currentSources.splice(to, 0, moved);
          renderSources(currentSources);
          persistOrder();
          /* The list was rebuilt, so focus has to be put back on the same control. */
          if (keepFocus) {
            const row = list.querySelector(`[data-source-id="${safeCss(moved.id)}"]`);
            const target = row?.querySelector(`${keepFocus}:not(:disabled)`) || row?.querySelector('.source-move button:not(:disabled)');
            target?.focus();
          }
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
            const state = source.blocked ? 'failing' : source.in_cooldown ? 'cooling' : health.status === 'healthy' ? 'healthy' : health.failures ? 'failing' : '';
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
              if (event.key === 'ArrowUp') { event.preventDefault(); moveSource(position, position - 1, '.grip'); }
              if (event.key === 'ArrowDown') { event.preventDefault(); moveSource(position, position + 1, '.grip'); }
            });

            const rank = document.createElement('span');
            rank.className = 'source-rank';
            rank.textContent = String(position + 1);

            const dot = document.createElement('span');
            dot.className = 'dot';

            const copy = document.createElement('span');
            copy.className = 'source-copy';
            const name = document.createElement('strong');
            name.textContent = source.label;
            const detail = document.createElement('small');
            detail.textContent = source.enabled === false ? 'Off · ' + (source.hint || '') : source.blocked || healthLabel(source);
            copy.append(name, detail);

            const move = document.createElement('span');
            move.className = 'source-move';
            [['up', -1], ['down', 1]].forEach(([word, delta]) => {
              const button = document.createElement('button');
              button.type = 'button';
              button.innerHTML = CHEVRON_ICON;
              button.dataset.direction = word;
              button.setAttribute('aria-label', `Move ${source.label} ${word}`);
              button.disabled = delta < 0 ? position === 0 : position === sources.length - 1;
              button.addEventListener('click', () => moveSource(position, position + delta, `[data-direction="${word}"]`));
              move.appendChild(button);
            });

            const toggle = document.createElement('input');
            toggle.type = 'checkbox';
            toggle.className = 'switch';
            toggle.setAttribute('role', 'switch');
            toggle.checked = source.enabled !== false;
            toggle.setAttribute('aria-label', `Use ${source.label}`);
            toggle.addEventListener('change', async () => {
              /* The switch shows what the server has, so a failed save puts it back. */
              if (!await setEnabled(source, toggle.checked)) toggle.checked = source.enabled !== false;
            });

            row.append(grip, rank, dot, copy, move, toggle);
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

        async function load() {
          try {
            const response = await fetch('/api/playback/sources');
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            const data = await response.json();
            preferences = data.preferences;
            applyPreferences();
            renderSources(data.sources || []);
            if (status && data.local_backend_degraded) status.textContent = 'Local Invidious is being skipped right now.';
          } catch (error) {
            list.replaceChildren();
            const failed = document.createElement('p');
            failed.className = 'set-foot';
            failed.textContent = `Could not load playback sources: ${failureCause(error)}.`;
            list.appendChild(failed);
          }
        }

        /* True once the server has it. onRejected takes the server's reason for refusing
           the value itself; any other failure is a toast. */
        async function save(patch, options = {}) {
          try {
            const response = await fetch('/api/playback/preferences', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify(patch)
            });
            if (response.status === 400 && options.onRejected) {
              const answer = await response.json().catch(() => ({}));
              options.onRejected(answer.detail || 'The server refused that value.');
              return false;
            }
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            preferences = (await response.json()).preferences;
            applyPreferences();
            /* Reordering already re-rendered optimistically; refetching would fight it. */
            if (!options.quiet) await load();
            showToast(options.quiet ? 'Playback order saved' : 'Playback settings saved', { kind: 'success' });
            return true;
          } catch (error) {
            showToast(`Could not save playback settings: ${failureCause(error)}`, { kind: 'error' });
            if (options.quiet) await load();
            return false;
          }
        }

        toggles.forEach((input) => {
          input.addEventListener('change', async () => {
            if (!await save({ [input.dataset.playbackToggle]: input.checked })) applyPreferences();
          });
        });

        const instanceInput = document.querySelector('[data-instance-input]');
        const instanceError = document.querySelector('[data-instance-error]');
        function showInstanceError(text) {
          if (instanceError) { instanceError.textContent = text; instanceError.hidden = !text; }
          if (text) instanceInput?.setAttribute('aria-invalid', 'true');
          else instanceInput?.removeAttribute('aria-invalid');
        }
        async function addInstance() {
          const value = (instanceInput?.value || '').trim();
          if (!value) return;
          showInstanceError('');
          const saved = await save({ public_invidious_urls: [...(preferences.public_invidious_urls || []), value] }, {
            onRejected: (reason) => { showInstanceError(reason); instanceInput?.focus(); }
          });
          if (saved) instanceInput.value = '';
        }
        instanceInput?.addEventListener('input', () => showInstanceError(''));
        document.querySelector('[data-instance-add]')?.addEventListener('click', addInstance);
        instanceInput?.addEventListener('keydown', (event) => {
          if (event.key === 'Enter') { event.preventDefault(); addInstance(); }
        });

        /* One request per source, so progress is a count of probes that really finished.
           The rows and the summary below come from those replies. */
        document.querySelector('[data-playback-test]')?.addEventListener('click', async (event) => {
          const button = event.currentTarget;
          const targets = currentSources.filter((source) => source.enabled !== false);
          if (!targets.length) {
            if (status) status.textContent = 'Turn on at least one source first.';
            return;
          }
          button.disabled = true;
          if (resultsBox) { resultsBox.hidden = true; resultsBox.replaceChildren(); }
          if (progress) progress.hidden = false;
          if (progressFill) progressFill.style.width = '0%';
          const rows = [];
          let working = 0;
          for (const [index, source] of targets.entries()) {
            const row = list.querySelector(`[data-source-id="${safeCss(source.id)}"]`);
            const detail = row?.querySelector('.source-copy small');
            if (status) status.textContent = `Testing ${index + 1} of ${targets.length}: ${source.label}`;
            row?.classList.add('is-testing');
            if (detail) detail.textContent = 'Testing';
            let note = '';
            let ok = false;
            let latency = NaN;
            try {
              const response = await fetch(`/api/playback/sources/test?source_id=${encodeURIComponent(source.id)}`, { method: 'POST' });
              if (!response.ok) throw new Error(`HTTP ${response.status}`);
              const result = (await response.json()).results?.[0] || {};
              ok = Boolean(result.ok);
              note = result.detail || '';
              latency = result.latency_ms;
            } catch (error) {
              note = failureCause(error);
            }
            if (ok) working += 1;
            row?.classList.remove('is-testing');
            if (detail) detail.textContent = ok ? `Worked in ${latency} ms` : `Failed${note ? `: ${note}` : ''}`;
            rows.push(hopRow(source.label, note, hopBadge(ok ? 'OK' : 'Failed', ok ? 'ok' : 'bad'), latency, ok ? '' : 'is-broken'));
            if (progressFill) progressFill.style.width = `${Math.round(((index + 1) / targets.length) * 100)}%`;
          }
          if (status) status.textContent = `${working} of ${targets.length} source${targets.length === 1 ? '' : 's'} can play video.`;
          if (resultsBox) {
            resultsBox.replaceChildren(hopTable([['Source'], ['Result'], ['Latency', true]], rows));
            resultsBox.hidden = false;
          }
          if (progress) progress.hidden = true;
          button.disabled = false;
          await load();
        });

        document.querySelector('[data-playback-diagnose]')?.addEventListener('click', async (event) => {
          const button = event.currentTarget;
          const box = document.querySelector('[data-playback-diagnosis]');
          const before = status?.textContent || '';
          button.disabled = true;
          if (status) status.textContent = 'Checking the playback chain';
          try {
            const response = await fetch('/api/playback/diagnose');
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            const data = await response.json();
            if (box) renderDiagnosis(box, data.diagnosis || {}, data.hops || []);
            if (status) status.textContent = data.diagnosis?.ok ? 'Local playback is healthy.' : before;
          } catch (error) {
            if (status) status.textContent = `Could not run the check: ${failureCause(error)}.`;
          } finally {
            button.disabled = false;
          }
        });

        document.querySelector('[data-settings-pane="playback"]')?.addEventListener('click', load);

        /* Refresh the health readout whenever the settings dialog is opened. */
        document.addEventListener('click', (event) => {
          if (event.target.closest('[data-open-settings-dialog]')) window.setTimeout(load, 60);
        });
        load();
      })();

      /* ---- settings dialog: panes, phone sub-screens, focus ---- */
      (() => {
        if (!settingsDialog) return;
        const dialog = settingsDialog;
        const tabs = [...dialog.querySelectorAll('[data-settings-pane]')];
        const panes = [...dialog.querySelectorAll('[data-settings-pane-body]')];
        const title = dialog.querySelector('[data-settings-title]');
        const scroller = dialog.querySelector('[data-settings-scroll]');
        const back = dialog.querySelector('[data-settings-back]');
        /* The same width that makes this dialog a sheet: the section list is its first screen. */
        const narrow = window.matchMedia('(max-width: 600px)');
        let returnFocus = null;

        function showPane(tab) {
          const key = tab.dataset.settingsPane;
          tabs.forEach((item) => {
            const active = item === tab;
            item.classList.toggle('active', active);
            item.setAttribute('aria-selected', active ? 'true' : 'false');
            item.tabIndex = active ? 0 : -1;
          });
          panes.forEach((pane) => pane.classList.toggle('active', pane.dataset.settingsPaneBody === key));
          if (title) title.textContent = tab.textContent.trim();
          if (scroller) scroller.scrollTop = 0;
          dialog.dataset.settingsView = 'pane';
        }

        function showList() {
          dialog.dataset.settingsView = 'list';
          tabs.find((tab) => tab.classList.contains('active'))?.focus({ preventScroll: true });
        }

        tabs.forEach((tab, index) => {
          tab.addEventListener('click', () => {
            showPane(tab);
            if (narrow.matches) back?.focus({ preventScroll: true });
          });
          tab.addEventListener('keydown', (event) => {
            const moves = { ArrowDown: index + 1, ArrowUp: index - 1, Home: 0, End: tabs.length - 1 };
            if (!(event.key in moves)) return;
            event.preventDefault();
            const next = tabs[(moves[event.key] + tabs.length) % tabs.length];
            next.click();
            next.focus();
          });
        });
        back?.addEventListener('click', showList);

        /* The back gesture: ui-kit asks before it closes a sheet. */
        dialog.addEventListener('sheet-back', (event) => {
          if (!narrow.matches || dialog.dataset.settingsView !== 'pane') return;
          event.preventDefault();
          showList();
        });

        /* trigger is what focus returns to. A menu row is gone by then, so its summary. */
        window.ofOpenSettings = (target, trigger) => {
          if (!dialog.open) {
            returnFocus = trigger?.closest('details')?.querySelector('summary') || trigger || null;
            dialog.dataset.settingsView = 'list';
            dialog.showModal();
          }
          const tab = target ? dialog.querySelector(`[data-settings-pane="${safeCss(target)}"]`) : null;
          tab?.click();
          if (narrow.matches) {
            (tab ? back : tabs.find((item) => item.classList.contains('active')))?.focus({ preventScroll: true });
          } else {
            tabs.find((item) => item.classList.contains('active'))?.focus({ preventScroll: true });
          }
        };
        dialog.addEventListener('close', () => {
          returnFocus?.focus({ preventScroll: true });
          returnFocus = null;
        });
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
        const videoLength = Number(document.querySelector('[data-watch-layout]')?.dataset.duration || 0);
        (item.content || '').split(/((?:\\d{1,2}:)?\\d{1,2}:\\d{2}|@[A-Za-z0-9._-]{2,30})/).forEach((chunk) => {
          if (!chunk) return;
          const stamp = /^(?:\\d{1,2}:)?\\d{1,2}:\\d{2}$/.test(chunk)
            ? chunk.split(':').reduce((total, part) => total * 60 + Number(part), 0)
            : -1;
          /* Past the end of the video it is a time of day or a score, not a position. */
          if (stamp >= 0 && (!videoLength || stamp <= videoLength)) {
            const link = document.createElement('button');
            link.type = 'button';
            link.className = 'ts-link';
            link.dataset.seek = chunk;
            link.textContent = chunk;
            body.appendChild(link);
            if (!row.dataset.firstStamp) row.dataset.firstStamp = String(stamp);
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
              const videoId = toggle.closest('[data-comments-for]')?.dataset.commentsFor || '';
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
          (row) => row.dataset.authorId && !row.querySelector(':scope > .avatar img'));
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
      /* Top and Newest are the source's own orderings. Timed is ours, over what has loaded. */
      let commentSort = 'top';
      let commentGeneration = 0;

      /* The API's own words, which name the cause; a status code alone would not. */
      async function commentFailure(response) {
        try {
          const data = await response.json();
          if (typeof data.detail === 'string') return `${data.detail}.`;
        } catch (error) { /* not JSON */ }
        return `Comments could not be loaded (HTTP ${response.status}).`;
      }

      function commentSkeleton(body) {
        const rows = [0, 1, 2].map(() => {
          const row = document.createElement('div');
          row.className = 'comment comment-skeleton';
          row.setAttribute('aria-hidden', 'true');
          row.innerHTML = '<span class="skeleton skeleton-avatar"></span><div><span class="skeleton skeleton-line is-meta"></span><span class="skeleton skeleton-line"></span></div>';
          return row;
        });
        body.replaceChildren(...rows);
      }

      function applyTimedSort(section) {
        const body = section.querySelector('[data-comments-body]');
        const note = section.querySelector('[data-comments-note]');
        const rows = [...body.querySelectorAll(':scope > .comment')];
        if (commentSort !== 'timed') {
          /* Back to the order they arrived in, which Timed rearranged in place. */
          rows.sort((a, b) => Number(a.dataset.arrival) - Number(b.dataset.arrival));
          rows.forEach((row) => { row.hidden = false; body.appendChild(row); });
          if (note) note.hidden = true;
          return;
        }
        const timed = rows.filter((row) => row.dataset.firstStamp)
          .sort((a, b) => Number(a.dataset.firstStamp) - Number(b.dataset.firstStamp));
        rows.forEach((row) => { row.hidden = !row.dataset.firstStamp; });
        timed.forEach((row) => body.appendChild(row));
        if (note) {
          note.hidden = false;
          note.textContent = `${timed.length} of the ${rows.length} loaded comments name a time in the video, earliest first.`;
        }
      }

      function paintCommentPreview(section, item) {
        const line = section.querySelector('[data-comments-preview]');
        if (!line) return;
        line.textContent = item ? `${String(item.author || '').replace(/^@/, '')}: ${item.content || ''}` : 'No comments on this video.';
      }

      async function loadComments(more = false) {
        const section = document.querySelector('.watch-layout [data-comments-for]');
        if (!section || commentsBusy || (more && (commentsDone || !commentCursor))) return;
        commentsBusy = true;
        const generation = commentGeneration;
        const videoId = section.dataset.commentsFor;
        const body = section.querySelector('[data-comments-body]');
        const counters = section.querySelectorAll('[data-comments-count]');
        try {
          const params = new URLSearchParams();
          if (more) params.set('continuation', commentCursor);
          if (commentSort === 'new') params.set('sort', 'new');
          const query = params.size ? `?${params}` : '';
          const response = await fetch(`/api/comments/${encodeURIComponent(videoId)}${query}`);
          if (!response.ok) throw new Error(await commentFailure(response));
          const data = await response.json();
          if (generation !== commentGeneration) return;
          const items = data.items || [];
          commentCursor = data.continuation || '';
          if (!commentCursor) commentsDone = true;
          if (!more) body.replaceChildren();
          if (data.comment_count != null) counters.forEach((counter) => { counter.textContent = formatCount(data.comment_count); });
          if (!more) paintCommentPreview(section, items[0]);
          if (!items.length) {
            if (more) { commentsDone = true; return; }
            const empty = document.createElement('p');
            empty.className = 'comments-state';
            empty.textContent = 'No comments on this video.';
            body.appendChild(empty);
            return;
          }
          if (!more && data.notice) {
            const notice = document.createElement('p');
            notice.className = 'comments-state';
            notice.dataset.commentsNotice = '';
            notice.textContent = data.notice;
            body.appendChild(notice);
          }
          items.forEach((item) => {
            const row = renderComment(item);
            row.dataset.arrival = String(body.children.length);
            body.appendChild(row);
          });
          applyTimedSort(section);
          resolveCommentAvatars(body);
        } catch (error) {
          if (generation !== commentGeneration) return;
          if (more) { commentsDone = true; return; }
          body.replaceChildren();
          const failed = document.createElement('p');
          failed.className = 'comments-state';
          failed.textContent = error.message || 'Comments could not be loaded right now.';
          body.appendChild(failed);
          const line = section.querySelector('[data-comments-preview]');
          if (line) line.textContent = failed.textContent;
        } finally {
          commentsBusy = false;
        }
      }
      loadComments();

      (() => {
        const section = document.querySelector('.watch-layout [data-comments-for]');
        if (!section) return;
        section.querySelectorAll('[data-comment-sort]').forEach((button) => {
          button.addEventListener('click', () => {
            const sort = button.dataset.commentSort;
            if (sort === commentSort) return;
            const previous = commentSort;
            commentSort = sort;
            section.querySelectorAll('[data-comment-sort]').forEach((item) => {
              item.setAttribute('aria-pressed', item === button ? 'true' : 'false');
            });
            /* Timed works on what is already here; the other two are a fresh fetch. */
            if (sort === 'timed' || (previous === 'timed' && sort === 'top' && !section.dataset.sortedNew)) {
              applyTimedSort(section);
              return;
            }
            section.dataset.sortedNew = sort === 'new' ? '1' : '';
            commentGeneration += 1;
            commentCursor = '';
            commentsDone = false;
            commentsBusy = false;
            applyTimedSort(section);
            commentSkeleton(section.querySelector('[data-comments-body]'));
            loadComments();
          });
        });
        if (!('IntersectionObserver' in window)) return;
        const sentinel = document.createElement('div');
        sentinel.className = 'feed-sentinel';
        const list = section.querySelector('[data-comments-body]');
        list.after(sentinel);
        /* Fetches before the sentinel is on screen, so the next page is usually already
           in place by the time you reach the bottom. */
        new IntersectionObserver((entries) => {
          if (entries.some((entry) => entry.isIntersecting)) loadComments(true);
        }, { rootMargin: '600px 0px' }).observe(sentinel);
        /* In the phone sheet the list scrolls inside itself, so the sentinel never moves. */
        list.addEventListener('scroll', () => {
          if (list.scrollTop + list.clientHeight > list.scrollHeight - 600) loadComments(true);
        }, { passive: true });
      })();

      /* ---- bottom sheet drag, for the two comment sheets that are not dialogs ---- */
      function dragToClose(handle, panel, close) {
        if (!handle || !panel) return;
        /* Touch adjustment hands a finger on the handle to the nearest button under it (the
           sort segments' enlarged hit area reaches into the handle), so the handle's own box
           decides, not the event target. */
        panel.addEventListener('pointerdown', (event) => {
          const box = handle.getBoundingClientRect();
          if (!box.height || event.clientY < box.top || event.clientY > box.bottom) return;
          const startY = event.clientY;
          const startTime = event.timeStamp;
          let distance = 0;
          handle.setPointerCapture(event.pointerId);
          panel.classList.add('is-dragging');
          const move = (moved) => {
            distance = Math.max(0, moved.clientY - startY);
            panel.style.transform = `translateY(${distance}px)`;
          };
          const finish = (ended) => {
            handle.removeEventListener('pointermove', move);
            handle.removeEventListener('pointerup', finish);
            handle.removeEventListener('pointercancel', finish);
            panel.classList.remove('is-dragging');
            panel.style.removeProperty('transform');
            if (ended.type === 'pointerup' && (distance > 80 || distance / Math.max(1, ended.timeStamp - startTime) > 0.5)) close();
          };
          handle.addEventListener('pointermove', move);
          handle.addEventListener('pointerup', finish);
          handle.addEventListener('pointercancel', finish);
        });
      }

      /* ---- comments sheet (phone) ---- */
      /* Deliberately not a modal dialog: that would make the player inert, and the point of
         this sheet is reading comments while the video keeps playing above it. */
      (() => {
        const section = document.querySelector('.watch-layout [data-comments-for]');
        const open = section?.querySelector('[data-comments-open]');
        const panel = section?.querySelector('[data-comments-panel]');
        if (!open || !panel) return;
        const close = section.querySelector('[data-comments-close]');
        const stage = document.querySelector('.watch-stage');

        function setOpen(on) {
          section.classList.toggle('is-sheet-open', on);
          open.setAttribute('aria-expanded', on ? 'true' : 'false');
          if (on) {
            /* Measured, not assumed: a Short's player is taller than a 16:9 one. */
            /* A landscape phone does not pin the player, so scrolled down its bottom edge can
               be above the screen; the sheet then starts at the top of the screen. */
            const bottom = stage ? stage.getBoundingClientRect().bottom : 0;
            panel.style.setProperty('--sheet-top', `${Math.round(Math.max(0, Math.min(bottom, window.innerHeight * 0.55)))}px`);
            history.pushState({ ofComments: true }, '');
            close?.focus({ preventScroll: true });
          } else {
            open.focus({ preventScroll: true });
          }
        }
        function dismiss() {
          if (!section.classList.contains('is-sheet-open')) return;
          setOpen(false);
          if (history.state?.ofComments) history.back();
        }
        open.addEventListener('click', () => setOpen(true));
        close?.addEventListener('click', dismiss);
        document.addEventListener('keydown', (event) => { if (event.key === 'Escape') dismiss(); });
        window.addEventListener('popstate', () => {
          if (section.classList.contains('is-sheet-open') && !history.state?.ofComments) setOpen(false);
        });
        window.matchMedia('(min-width: 901px)').addEventListener('change', (event) => { if (event.matches) dismiss(); });
        dragToClose(section.querySelector('[data-comments-handle]'), panel, dismiss);
      })();

      /* ---- chapters follow the playhead ---- */
      (() => {
        const rows = [...document.querySelectorAll('[data-chapter-start]')];
        if (!rows.length) return;
        const player = document.querySelector('.player-frame');
        let current = null;
        function mark(seconds) {
          let row = null;
          for (const candidate of rows) {
            if (Number(candidate.dataset.chapterStart) > seconds) break;
            row = candidate;
          }
          if (row === current) return;
          current?.removeAttribute('aria-current');
          current = row;
          if (!row) return;
          row.setAttribute('aria-current', 'true');
          /* Scrolls the list only, never the page under someone reading it. */
          const list = row.closest('[data-chapter-list]');
          if (list) list.scrollTop = row.offsetTop - list.offsetTop - (list.clientHeight - row.offsetHeight) / 2;
        }
        window.addEventListener('message', (event) => {
          if (event.origin !== window.location.origin || event.source !== player?.contentWindow) return;
          if ((event.data || {}).type === 'openfeed:clock') mark(Number(event.data.seconds) || 0);
        });
        mark(Number(document.querySelector('[data-watch-layout]')?.dataset.resume || 0));
      })();

      /* ---- up next filter ---- */
      (() => {
        const buttons = document.querySelectorAll('[data-upnext-filter]');
        if (!buttons.length) return;
        const channel = document.querySelector('[data-watch-channel]')?.dataset.watchChannel || '';
        buttons.forEach((button) => button.addEventListener('click', () => {
          const only = button.dataset.upnextFilter === 'channel';
          buttons.forEach((item) => item.setAttribute('aria-pressed', item === button ? 'true' : 'false'));
          document.querySelectorAll('#tab-suggestions [data-video-card]').forEach((row) => {
            row.hidden = only && row.dataset.channelId !== channel;
          });
        }));
      })();

      /* ---- list playback (Play all / Shuffle) ---- */
      (() => {
        const list = document.querySelector('[data-watch-list]');
        if (!list) return;
        const items = list.querySelector('.watch-list-items');
        const current = list.querySelector('[aria-current="true"]');
        /* Scrolls the list only, never the page. */
        if (items && current) items.scrollTop = current.offsetTop - items.offsetTop - items.clientHeight / 3;
        const next = list.querySelector('[data-list-next] a[href^="/watch/"]');
        if (!next) return;
        /* The player reports its clock up to this page, including the pause that comes with
           the end of a video. Only after it has actually played, so a resume point at the
           very end does not skip the video before it starts. */
        let played = false;
        let leaving = false;
        window.addEventListener('message', (event) => {
          const frame = document.querySelector('.player-frame');
          if (leaving || event.origin !== window.location.origin || !frame || event.source !== frame.contentWindow) return;
          const data = event.data || {};
          if (data.type !== 'openfeed:clock') return;
          if (!data.paused) { played = true; return; }
          const duration = Number(data.duration) || 0;
          if (!played || !duration || Number(data.seconds) < duration - 0.75) return;
          leaving = true;
          window.ofNavPending?.();
          window.location.href = next.href;
        });
      })();

      /* ---- open diagnostics, asked for by the failure card inside the player ---- */
      window.addEventListener('message', (event) => {
        if (event.origin !== window.location.origin || (event.data || {}).type !== 'openfeed:open-diagnostics') return;
        const dialog = document.querySelector('#settings-dialog');
        if (!dialog) { window.location.href = '/?settings=playback'; return; }
        if (!dialog.open) dialog.showModal();
        document.querySelector('[data-settings-pane="playback"]')?.click();
        document.querySelector('[data-playback-diagnose]')?.click();
      });

      /* ---- sidebar ----
         Above 1100px the hamburger folds the sidebar to the 64px rail and the choice
         persists. From 901 to 1100px the rail is all there is room for, so it opens the
         full sidebar over the page instead. Watch and Shorts start with it out of the way
         and open it the same way. At 900px and below the tab bar replaces all of this. */
      (() => {
        const root = document.documentElement;
        const body = document.body;
        const immersive = body.classList.contains('immersive-nav');
        const compact = window.matchMedia('(max-width: 1100px)');
        const toggle = document.querySelector('[data-nav-toggle]');
        const scrim = document.querySelector('[data-nav-scrim]');

        const readPreference = () => {
          try { return localStorage.getItem('openfeed-nav'); } catch (error) { return null; }
        };
        const writePreference = (value) => {
          try { localStorage.setItem('openfeed-nav', value); } catch (error) { /* stays for this page */ }
        };
        const isOverlaid = () => (immersive ? !body.classList.contains('nav-collapsed') : body.classList.contains('nav-drawer'));
        const syncToggle = () => {
          const open = immersive || compact.matches ? isOverlaid() : !root.classList.contains('nav-rail');
          toggle?.setAttribute('aria-expanded', open ? 'true' : 'false');
        };
        /* Slid off-canvas on Watch and Shorts, the sidebar must leave the tab order too, or
           Tab walks through a column of invisible links before reaching the page. */
        const sidebar = document.querySelector('.desktop-sidebar');
        const syncInert = () => { if (sidebar && immersive) sidebar.inert = body.classList.contains('nav-collapsed'); };
        function setOverlay(open) {
          if (immersive) body.classList.toggle('nav-collapsed', !open);
          else body.classList.toggle('nav-drawer', open);
          syncInert();
          syncToggle();
        }
        function syncWidth() {
          body.classList.remove('nav-drawer');
          root.classList.toggle('nav-rail', compact.matches || readPreference() === 'collapsed');
          syncToggle();
        }

        if (immersive) body.classList.add('nav-collapsed', 'nav-overlay');
        syncInert();
        syncWidth();
        /* Only animate after the first paint so the initial state does not slide in. */
        window.requestAnimationFrame(() => body.classList.add('nav-ready'));

        toggle?.addEventListener('click', () => {
          if (immersive || compact.matches) {
            setOverlay(!isOverlaid());
            return;
          }
          const rail = !root.classList.contains('nav-rail');
          root.classList.toggle('nav-rail', rail);
          writePreference(rail ? 'collapsed' : 'open');
          syncToggle();
        });
        scrim?.addEventListener('click', () => setOverlay(false));
        document.addEventListener('keydown', (event) => {
          if (event.key === 'Escape' && isOverlaid()) setOverlay(false);
        });
        compact.addEventListener('change', syncWidth);
      })();

      /* ---- theater mode ---- */
      (() => {
        const toggle = document.querySelector('[data-theater-toggle]');
        if (!toggle) return;
        const body = document.body;
        function apply(on) {
          body.classList.toggle('theater', on);
          toggle.setAttribute('role', 'menuitemcheckbox');
          toggle.setAttribute('aria-checked', on ? 'true' : 'false');
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
          dialog.querySelector('[data-download-actions]').hidden = !show || !chosenOption();
          if (!show) dialog.querySelector('[data-download-warning]').hidden = true;
          else refreshChoice();
        }

        function chosenOption() {
          return dialog.querySelector('[data-download-options] input:checked')?.closest('[data-download-mode]') || null;
        }

        /* The label says where the file goes, and the warning only ever speaks from a size
           the backends reported and free space the server measured. A direct download to
           this device never lands on the server disk, so it has nothing to warn about. */
        function refreshChoice() {
          const option = chosenOption();
          const destination = chosenDestination();
          dialog.querySelector('[data-download-start]').textContent = destination === 'server' ? 'Keep on server' : 'Download';
          const warning = dialog.querySelector('[data-download-warning]');
          const size = Number(option?.dataset.downloadSize || 0);
          const free = Number(dialog.dataset.freeBytes || 0);
          const landsOnServer = destination === 'server' || option?.dataset.downloadMode !== 'direct';
          const tight = Boolean(option && landsOnServer && size && dialog.dataset.freeBytes && size > free * 0.9);
          warning.hidden = !tight;
          warning.textContent = tight
            ? `This needs about ${formatSize(size)} and the server has ${formatSize(free)} free. It may fail partway.`
            : '';
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
          dialog.dataset.freeBytes = data.free_bytes ?? '';
          if (!data.ok || !(data.formats || []).length) {
            const empty = document.createElement('p');
            empty.className = 'dialog-note';
            empty.textContent = data.detail || 'No downloadable stream is available for this video.';
            list.appendChild(empty);
            note.hidden = true;
            showOptions(true);
            /* Nothing to save means nowhere to save it: the two destination cards would
               only look selectable next to a dialog with no Download button. */
            dialog.querySelector('[data-download-destination]').hidden = true;
            subtitle.textContent = data.title || '';
            return;
          }
          subtitle.textContent = data.title || 'Choose a quality.';
          data.formats.forEach((format, index) => {
            const option = document.createElement('label');
            option.className = 'download-option';
            option.dataset.downloadMode = format.mode || 'direct';
            option.dataset.downloadItag = format.itag || '';
            option.dataset.downloadHeight = String(format.height || 0);
            option.dataset.downloadContainer = format.container || 'mp4';
            option.dataset.downloadMerge = format.needs_merge ? '1' : '';
            option.dataset.downloadFilename = format.filename || '';
            option.dataset.downloadQuality = format.quality || '';
            option.dataset.downloadSize = String(format.size_bytes || 0);
            option.innerHTML = '<input class="sr-only" type="radio" name="download-quality"><span class="download-radio" aria-hidden="true"></span><span class="download-option-copy"></span><span class="download-size"></span>';
            option.querySelector('input').checked = index === 0;
            const copy = option.querySelector('.download-option-copy');
            const title = document.createElement('strong');
            title.textContent = format.quality || 'Unknown quality';
            const detail = document.createElement('small');
            detail.textContent = format.needs_merge
              ? `${(format.container || 'mp4').toUpperCase()} \\u00b7 merged on the server`
              : `${(format.container || 'mp4').toUpperCase()} \\u00b7 streams straight through`;
            copy.append(title, detail);
            /* A merged size is derived from the tracks yt-dlp is expected to pick, so it
               is close but not exact — the tilde says so rather than implying precision. */
            const size = formatSize(format.size_bytes);
            option.querySelector('.download-size').textContent = size && format.size_estimated ? `~${size}` : size;
            list.appendChild(option);
          });
          showOptions(true);
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
          refreshChoice();
        });

        dialog?.addEventListener('change', (event) => {
          if (event.target.matches('[data-download-options] input')) refreshChoice();
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

          const option = event.target.closest('[data-download-start]') ? chosenOption() : null;
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

          button.closest('.yt-menu')?.removeAttribute('open');
          dialog.dataset.videoId = videoId;
          dialog.dataset.videoTitle = button.dataset.videoTitle || '';
          stopPolling();
          activeJob = '';
          showOptions(true);
          dialog.querySelector('[data-download-options]').innerHTML = '<span class="sr-only">Checking what is available</span>' + '<span class="skeleton download-option-skeleton"></span>'.repeat(3);
          dialog.querySelector('[data-download-note]').hidden = true;
          showOptions(true);
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
            const response = await fetch(`/api/videos/${encodeURIComponent(videoId)}/dislikes`);
            if (!response.ok) throw new Error('poll failed');
            const data = await response.json();
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
            /* A transient network error must not kill the poll; the attempt cap is
               what bounds it. */
            return;
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
        /* Offsets count stored rows, and a sync mid-scroll can shift them, so a card can
           come back. The grid shows each video once whatever the server sends. */
        const seen = new Set(Array.from(grid.querySelectorAll('[data-video-card]')).map((card) => card.dataset.videoId));

        async function loadMore() {
          if (loading || done) return;
          loading = true;
          /* The empty state has already answered, so placeholders under it would be a
             second, contradictory answer. The request still runs: it asks the channel
             itself and can find what the stored list did not have. */
          const empty = grid.querySelector('.empty-state');
          if (spinner && !empty) spinner.hidden = false;
          try {
            const url = `/api/channels/${encodeURIComponent(grid.dataset.channelId)}/videos/html`
              + `?offset=${offset}&sort=${encodeURIComponent(grid.dataset.sort || 'latest')}`
              + `&kind=${encodeURIComponent(grid.dataset.kind || 'videos')}`
              + `&q=${encodeURIComponent(grid.dataset.q || '')}`;
            const data = await fetch(url).then((r) => r.json());
            if (data.html) {
              const doc = new DOMParser().parseFromString(data.html, 'text/html');
              const fresh = Array.from(doc.body.children).filter((card) => {
                const id = card.dataset.videoId;
                if (!id || seen.has(id)) return false;
                seen.add(id);
                return true;
              });
              if (fresh.length) empty?.remove();
              grid.append(...fresh);
            }
            /* An empty page with more behind it (a sparse Shorts window) still
               advances: the offset moves but nothing renders. */
            const from = offset;
            grid.dataset.nextOffset = String(data.next_offset ?? offset);
            offset = Number(grid.dataset.nextOffset || 0);
            /* Stop only when the server says the well is dry, or when it would be asked
               the same thing again. An empty page alone is not the end (Shorts windows
               can scan past long-form videos), as long as the offset moved. */
            if (!data.has_more || (offset === from && !data.html)) done = true;
          } catch (error) {
            done = true;
          } finally {
            loading = false;
            window.ofHideLoader(spinner);
            if (!done) observeAgain();
          }
        }

        /* rootMargin starts the fetch before the sentinel is actually visible, so the
           next batch is usually already in place by the time the user reaches it. */
        const observer = new IntersectionObserver((entries) => {
          if (entries.some((entry) => entry.isIntersecting)) loadMore();
        }, { rootMargin: '800px 0px' });
        /* An observer reports changes only, so a page too short to move the sentinel out
           of range would end the scroll. Observing afresh reports where it is now. */
        function observeAgain() {
          observer.unobserve(sentinel);
          observer.observe(sentinel);
        }
        observer.observe(sentinel);
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
          Array.from(list.querySelectorAll('[data-video-id], [data-result-id]')).map((el) => el.dataset.videoId || el.dataset.resultId)
        );
        let total = Number(list.dataset.count || 0);
        const heading = document.querySelector('[data-search-head] h1');
        const query = new URLSearchParams({
          q: list.dataset.q || '', type: list.dataset.type || 'all', sort: list.dataset.sort || 'relevance',
          date: list.dataset.date || '', duration: list.dataset.duration || '', features: list.dataset.features || ''
        });
        function syncHeading() {
          if (heading) heading.textContent = `${total}${done ? '' : '+'} result${total === 1 ? '' : 's'} for "${list.dataset.q || ''}"`;
        }

        async function loadMore() {
          if (loading || done) return;
          loading = true;
          if (spinner) spinner.hidden = false;
          let added = 0;
          try {
            query.set('page', String(page));
            const data = await fetch(`/api/search/html?${query}`).then((r) => r.json());
            if (data.html) {
              const doc = new DOMParser().parseFromString(data.html, 'text/html');
              /* Menus nest in a row and carry the same data-video-id, so take rows only. */
              Array.from(doc.body.children)
                .filter((el) => {
                  const id = el.dataset.videoId || el.dataset.resultId;
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
            window.ofHideLoader(spinner);
            total += added;
            syncHeading();
            if (!done) observeAgain();
          }
        }

        const observer = new IntersectionObserver((entries) => {
          if (entries.some((entry) => entry.isIntersecting)) loadMore();
        }, { rootMargin: '800px 0px' });
        /* A sparse page (rows already shown) leaves the sentinel in range, and an observer
           reports changes only, so the scroll would stall with more behind it. Observing
           afresh reports where it is now; the server ends the run at its last page. */
        function observeAgain() {
          observer.unobserve(sentinel);
          observer.observe(sentinel);
        }
        observer.observe(sentinel);
      })();

      /* ---- takeout import ---- */
      (() => {
        const drop = document.querySelector('[data-import-drop]');
        if (!drop) return;
        const input = document.querySelector('[data-import-file]');
        const chosen = document.querySelector('[data-import-chosen]');
        const progress = document.querySelector('[data-import-progress]');
        const fill = document.querySelector('[data-import-bar-fill]');
        const state = document.querySelector('[data-import-state]');
        const result = document.querySelector('[data-import-result]');
        let file = null;

        function show(node, on) { if (node) node.hidden = !on; }

        function setStage(stage) {
          document.querySelectorAll('[data-import-stage]').forEach((item) => {
            const number = Number(item.dataset.importStage);
            item.classList.toggle('is-done', number < stage);
            item.classList.toggle('is-current', number === stage);
            if (number === stage) item.setAttribute('aria-current', 'step');
            else item.removeAttribute('aria-current');
          });
        }

        function fail(message) {
          show(progress, false);
          show(drop, !file);
          show(chosen, Boolean(file));
          setStage(document.querySelector('[data-import-status]') ? 3 : 1);
          result.textContent = message;
          show(result, true);
        }

        function choose(picked) {
          if (!picked) return;
          show(result, false);
          if (!picked.name.toLowerCase().endsWith('.zip')) {
            file = null;
            show(chosen, false);
            fail('That is not a .zip archive. Upload the file Takeout gave you, unmodified.');
            return;
          }
          file = picked;
          document.querySelector('[data-import-name]').textContent = picked.name;
          document.querySelector('[data-import-size]').textContent = picked.size < 1048576 ? `${Math.ceil(picked.size / 1024)} KB` : `${(picked.size / 1048576).toFixed(1)} MB`;
          show(drop, false);
          show(chosen, true);
          document.querySelector('[data-import-start]')?.focus();
        }

        /* XMLHttpRequest because fetch reports no upload progress. Parsing has no signal to
           report, so past 100% the bar stays full and the copy says what is happening. */
        function upload() {
          return new Promise((resolve, reject) => {
            const body = new FormData();
            body.append('archive', file);
            const request = new XMLHttpRequest();
            request.open('POST', '/api/takeout/import');
            request.responseType = 'json';
            request.upload.addEventListener('progress', (event) => {
              if (!event.lengthComputable) return;
              const percent = Math.round((event.loaded / event.total) * 100);
              if (fill) fill.style.width = `${percent}%`;
              if (state) state.textContent = `Uploading ${percent}%`;
            });
            request.upload.addEventListener('load', () => {
              if (fill) fill.style.width = '100%';
              if (state) state.textContent = 'Reading your archive. Large exports take a while.';
            });
            request.addEventListener('load', () => resolve({ status: request.status, data: request.response }));
            request.addEventListener('error', () => reject(new Error('network')));
            request.send(body);
          });
        }

        document.querySelector('[data-import-start]')?.addEventListener('click', async () => {
          if (!file) return;
          show(chosen, false);
          show(result, false);
          if (fill) fill.style.width = '0%';
          if (state) state.textContent = 'Uploading';
          show(progress, true);
          setStage(2);
          try {
            const { status, data } = await upload();
            if (status >= 200 && status < 300) {
              /* Everything the Imported stage shows is server state. */
              window.location.assign(`${window.location.pathname}?settings=import`);
              return;
            }
            fail(data?.detail || `That archive could not be imported: the server answered HTTP ${status}.`);
          } catch (error) {
            fail('The upload failed. If the archive is very large, try exporting only history and subscriptions.');
          }
        });
        document.querySelector('[data-import-clear]')?.addEventListener('click', () => {
          file = null;
          if (input) input.value = '';
          show(chosen, false);
          show(drop, true);
          drop.focus();
        });

        drop.addEventListener('click', () => input?.click());
        drop.addEventListener('keydown', (event) => {
          if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); input?.click(); }
        });
        input?.addEventListener('click', (event) => event.stopPropagation());
        input?.addEventListener('change', () => choose(input.files?.[0]));
        ['dragenter', 'dragover'].forEach((name) => drop.addEventListener(name, (event) => {
          event.preventDefault(); drop.classList.add('is-over');
        }));
        ['dragleave', 'drop'].forEach((name) => drop.addEventListener(name, () => drop.classList.remove('is-over')));
        drop.addEventListener('drop', (event) => {
          event.preventDefault();
          choose(event.dataTransfer?.files?.[0]);
        });

        document.querySelector('[data-takeout-delete]')?.addEventListener('click', async (event) => {
          /* currentTarget is cleared once the event finishes dispatching, so take it before the await. */
          const button = event.currentTarget;
          const videos = Number(button.dataset.importVideos || 0).toLocaleString();
          const channels = Number(button.dataset.importChannels || 0).toLocaleString();
          if (!await window.ofConfirm({
            title: 'Undo this import?',
            body: `Deletes the imported record of ${videos} videos and ${channels} channels. This cannot be undone: you would need the archive to import it again. Your own OpenFeed history and any subscriptions you already added stay.`,
            confirmLabel: 'Delete imported data',
            danger: 'filled',
            typed: 'delete import',
          })) return;
          button.disabled = true;
          try {
            const response = await fetch('/api/takeout', { method: 'DELETE' });
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            document.querySelector('[data-import-status]')?.remove();
            setStage(1);
            showToast('Imported data deleted', { kind: 'success' });
          } catch (error) {
            button.disabled = false;
            showToast(`Could not delete the imported data: ${failureCause(error)}`, { kind: 'error' });
          }
        });
      })();

      /* ---- settings deep link ---- */
      (() => {
        const target = new URLSearchParams(window.location.search).get('settings');
        if (!target) return;
        /* Fires after the pane wiring below has bound, so switching panes works. */
        window.setTimeout(() => {
          window.ofOpenSettings?.(target);
          /* Drop the parameter so a reload does not reopen the dialog. */
          window.history.replaceState(null, '', window.location.pathname);
        }, 0);
      })();

      /* ---- sidebar: show more subscriptions ---- */
      function setSubscriptionNavExpanded(nav, expand) {
        nav.querySelectorAll('.nav-channel-extra').forEach((row) => { row.hidden = !expand; });
        const toggle = nav.querySelector('[data-subscription-more]');
        if (!toggle) return;
        toggle.setAttribute('aria-expanded', expand ? 'true' : 'false');
        const label = toggle.querySelector('span');
        if (label) label.textContent = expand ? toggle.dataset.lessLabel : toggle.dataset.moreLabel;
      }
      document.addEventListener('click', (event) => {
        const button = event.target.closest('[data-subscription-more]');
        if (!button || button.disabled) return;
        const nav = button.closest('[data-subscription-nav]');
        if (!nav) return;
        const expanded = button.getAttribute('aria-expanded') === 'true';
        if (!expanded && nav.dataset.subscriptionsLoaded !== 'true') {
          /* The shell renders a VISIBLE + 1 probe so page renders stay cheap;
             the full list (up to 250) arrives on first expand. */
          button.disabled = true;
          fetch('/api/sidebar/subscriptions', { headers: { 'Accept': 'text/html' } })
            .then((response) => { if (!response.ok) throw new Error('no list'); return response.text(); })
            .then((markup) => {
              const parsed = document.createRange().createContextualFragment(markup.trim()).firstElementChild;
              if (!parsed) throw new Error('no list');
              nav.replaceWith(parsed);
              parsed.dataset.subscriptionsLoaded = 'true';
              setSubscriptionNavExpanded(parsed, true);
            })
            .catch(() => { setSubscriptionNavExpanded(nav, true); })
            .finally(() => { button.disabled = false; });
          return;
        }
        setSubscriptionNavExpanded(nav, !expanded);
      });

      /* ---- dearrow settings ---- */
      document.querySelector('[data-setting-dearrow]')?.addEventListener('change', async (event) => {
        const input = event.currentTarget;
        const on = input.checked;
        const saved = await saveControl(input, { dearrow_enabled: on }, on ? 'DeArrow on. Titles change on the next page you open.' : 'DeArrow off', 'DeArrow', () => { input.checked = !on; });
        if (saved) document.querySelector('[data-dearrow-options]').hidden = !on;
      });
      bindPreferenceSwitch('[data-setting-dearrow-thumbnails]', (on) => ({ dearrow_thumbnails: on }), ['Community thumbnails on. Frames appear as this server cuts them.', 'Community thumbnails off'], 'Community thumbnails');
      document.querySelector('[data-setting-dearrow-casing]')?.addEventListener('change', (event) => {
        const select = event.currentTarget;
        const previous = select.value === 'lowercase' ? '' : 'lowercase';
        saveControl(select, { dearrow_casing: select.value }, `Title casing: ${select.selectedOptions[0]?.textContent}`, 'Title casing', () => { select.value = previous; });
      });

      /* ---- sponsorblock settings ---- */
      document.querySelector('[data-setting-sponsorblock]')?.addEventListener('change', async (event) => {
        const input = event.currentTarget;
        const on = input.checked;
        const saved = await saveControl(input, { sponsorblock_enabled: on }, on ? 'SponsorBlock on' : 'SponsorBlock off', 'SponsorBlock', () => { input.checked = !on; });
        if (saved) document.querySelector('[data-sponsorblock-categories]').hidden = !on;
      });

      document.addEventListener('change', (event) => {
        const select = event.target.closest('[data-sponsorblock-category]');
        if (!select) return;
        const actions = {};
        document.querySelectorAll('[data-sponsorblock-category]').forEach((node) => {
          actions[node.dataset.sponsorblockCategory] = node.value;
        });
        const previous = select.dataset.saved ?? [...select.options].find((option) => option.defaultSelected)?.value;
        const label = select.getAttribute('aria-label') || 'this change';
        saveControl(select, { sponsorblock_actions: actions }, `${label.replace(/ action$/, '')}: ${select.selectedOptions[0]?.textContent}`, label, () => { select.value = previous; })
          .then((saved) => { if (saved) select.dataset.saved = select.value; });
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
          showToast(clear ? 'Answer cleared' : 'Saved here only, not sent to YouTube');
        } catch (error) {
          showToast('Could not save your answer', { kind: 'error' });
        }
      });

      /* ---- feed controls ---- */
      (() => {
        /* Measured panels load when the dialog opens, not on every page render. */
        const dialog = document.querySelector('#feed-dialog');
        if (!dialog) return;
        let loaded = false;
        /* Every key the ranker can write into score_debug. A key missing here still shows,
           under its own name, because hiding a signal that fired would misreport the feed. */
        const SIGNALS = {
          subscription_source: ['From your subscriptions', 'Comes through a channel you follow.'],
          subscribed_channel: ['Subscribed channel', 'From a channel you follow.'],
          subscription_topic_affinity: ['Subscription topics', 'Shares topics with channels you follow.'],
          semantic_affinity: ['Topic similarity', 'Close to the subjects in what you watch.'],
          interest_exemplar: ['Close to videos you watched', 'Similar to specific videos in your history.'],
          token_interest_match: ['Title words', 'Words in the title match your interests.'],
          watched_channel: ['Channels you watch', 'You have spent time with this creator.'],
          watched_channel_loyalty: ['Channels you return to', 'You keep coming back to this creator.'],
          watched_topic: ['Subjects you watch', 'Matches subjects you spend time on.'],
          takeout_channel: ['Imported history', 'The channel appears in your Takeout import.'],
          preferred_topic: ['Followed topics', 'Matches a topic under More of.'],
          discovery_balance: ['Discovery balance', 'Moved up or down by the discovery setting.'],
          muted_topic: ['Muted topics', 'Matches a topic under Less of. Pushed down.'],
          takeout_already_watched: ['Already watched', 'In your imported history. Pushed down.'],
          quick_abandonment: ['Videos you bounced off', 'You left this one early. Pushed down.'],
          partial_watch_penalty: ['Part-watched', 'You started this and did not finish. Pushed down.'],
          ungrounded_discovery_penalty: ['Unmatched discovery', 'Nothing ties it to your interests. Pushed down.'],
          unknown_quality_penalty: ['Unknown quality', 'Too little data to judge it. Pushed down.'],
          undated_discovery_penalty: ['No publish date', 'Discovered with no date. Pushed down.'],
          graph_walk: ['Related-list links', 'Linked through YouTube related lists to channels you follow or videos you finished.'],
          velocity_prior: ['Popular and recent', 'Views weighed against age.'],
          channel_demotion: ['Creators you turned down', 'Two or more of their videos hidden or disliked. Halved.'],
        };

        function caption(text) {
          const node = document.createElement('p');
          node.className = 'fc-caption';
          node.textContent = text;
          return node;
        }

        function fail(error) {
          const message = `Could not measure your feed: ${failureCause(error)}.`;
          const retry = document.createElement('button');
          retry.type = 'button';
          retry.className = 'btn btn-sm';
          retry.textContent = 'Try again';
          retry.addEventListener('click', loadComposition);
          const line = caption(message);
          dialog.querySelector('[data-fc-composition]')?.replaceChildren(line, retry);
          dialog.querySelector('[data-fc-signals]')?.replaceChildren(caption('Not measured.'));
          dialog.querySelector('[data-fc-channels]')?.replaceChildren(caption('Not measured.'));
        }

        function paintComposition(cov) {
          const box = dialog.querySelector('[data-fc-composition]');
          if (!box) return;
          const total = cov.videos || 0;
          if (!total) {
            box.replaceChildren(caption('Nothing is ranked yet, so there is nothing to measure. Add a subscription or import Takeout, then run a sync.'));
            return;
          }
          const subs = Math.round(((cov.sources?.subscription || 0) / total) * 100);
          const measure = document.createElement('div');
          measure.className = 'fc-measure';
          measure.innerHTML = `<div class="fc-bar" role="img"><span class="fc-bar-fill"></span></div>
            <div class="fc-legend"><span><i class="fc-dot subs"></i>From channels you follow <b></b></span>
            <span><i class="fc-dot disc"></i>Discovered <b></b></span></div>`;
          measure.querySelector('.fc-bar').setAttribute('aria-label', `${subs}% from channels you follow, ${100 - subs}% discovered`);
          measure.querySelector('.fc-bar-fill').style.width = `${subs}%`;
          const [follow, found] = measure.querySelectorAll('b');
          follow.textContent = `${subs}%`;
          found.textContent = `${100 - subs}%`;
          measure.appendChild(caption(`Counted from the ${total} videos ranked for you right now: ${cov.distinct_channels || 0} different creators, and no single one holds more than ${Math.round((cov.top_channel_share || 0) * 100)}% of the feed.`));
          box.replaceChildren(measure);
        }

        function paintSignals(cov) {
          const box = dialog.querySelector('[data-fc-signals]');
          if (!box) return;
          const entries = Object.entries(cov.signal_coverage || {}).sort((a, b) => b[1] - a[1]);
          if (!entries.length) {
            box.replaceChildren(caption('No signals fired, because nothing is ranked yet.'));
            return;
          }
          box.replaceChildren(...entries.map(([name, share]) => {
            const [label, note] = SIGNALS[name] || [name.replace(/_/g, ' '), ''];
            const percent = Math.round(share * 100);
            const row = document.createElement('div');
            row.className = 'fc-signal-row';
            row.innerHTML = '<div class="fc-signal-copy"><strong></strong><small></small></div><b></b><span class="fc-meter" aria-hidden="true"><i></i></span>';
            row.querySelector('strong').textContent = label;
            row.querySelector('small').textContent = note;
            row.querySelector('b').textContent = `${percent}%`;
            row.querySelector('i').style.width = `${percent}%`;
            return row;
          }));
        }

        function paintChannels(rows) {
          const channels = dialog.querySelector('[data-fc-channels]');
          if (!channels) return;
          channels.innerHTML = '';
          /* Keys come from channel ids/titles stored from feeds, so build via DOM -
             attribute values set with setAttribute can never break out of the markup. */
          rows.forEach((row) => {
            if (!row || typeof row.key !== 'string') return;
            const button = document.createElement('button');
            button.className = row.muted ? 'fc-channel is-muted' : 'fc-channel';
            button.type = 'button';
            button.setAttribute('data-mute-channel', row.key);
            button.setAttribute('aria-pressed', row.muted ? 'true' : 'false');
            button.innerHTML = '<span></span><small></small><i class="fc-channel-mark">Muted</i>';
            button.querySelector('span').textContent = row.title || '';  /* text, never markup */
            button.querySelector('small').textContent = String(row.count || 0);
            channels.appendChild(button);
          });
          if (!channels.children.length) channels.replaceChildren(caption('No creators in the feed yet.'));
          paintMuted(rows.filter((row) => row && row.muted && typeof row.key === 'string'));
        }

        /* Every muted creator with its own Unmute, including ones muted from a card's
           "Don't recommend channel", which never appear among the creators filling the feed. */
        function paintMuted(rows) {
          const box = dialog.querySelector('[data-fc-muted]');
          const list = dialog.querySelector('[data-fc-muted-list]');
          if (!box || !list) return;
          list.replaceChildren(...rows.map((row) => {
            const item = document.createElement('li');
            item.className = 'fc-muted-row';
            item.innerHTML = '<span class="fc-muted-name"></span><button class="btn btn-sm" type="button">Unmute</button>';
            item.querySelector('span').textContent = row.title || row.key;
            const button = item.querySelector('button');
            button.setAttribute('data-unmute-channel', row.key);
            button.setAttribute('aria-label', `Unmute ${row.title || row.key}`);
            return item;
          }));
          box.hidden = !rows.length;
        }

        async function loadComposition() {
          if (loaded) return;
          loaded = true;
          try {
            const response = await fetch('/api/feed/composition');
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            const data = await response.json();
            const cov = data.coverage || {};
            paintComposition(cov);
            paintSignals(cov);
            paintChannels(data.channels || []);
          } catch (error) {
            loaded = false;
            fail(error);
          }
        }
        document.addEventListener('click', (event) => {
          if (event.target.closest('[data-open-feed-dialog]')) loadComposition();
        });
        /* What it measured may have changed by the next open. */
        dialog.addEventListener('close', () => { loaded = false; });
      })();

      /* Feed controls → Muted creators follows the pills: a mute adds its row with Unmute at
         once, an unmute takes it away. Keys compare case-insensitively, as the server stores them. */
      function syncMutedList(key, name, muted) {
        const list = document.querySelector('#feed-dialog [data-fc-muted-list]');
        if (!list) return;
        const lower = String(key).toLowerCase();
        list.querySelectorAll('[data-unmute-channel]').forEach((button) => {
          if (button.dataset.unmuteChannel.toLowerCase() === lower) button.closest('.fc-muted-row')?.remove();
        });
        if (muted) {
          const item = document.createElement('li');
          item.className = 'fc-muted-row';
          item.innerHTML = '<span class="fc-muted-name"></span><button class="btn btn-sm" type="button">Unmute</button>';
          item.querySelector('span').textContent = name;
          const button = item.querySelector('button');
          button.setAttribute('data-unmute-channel', key);
          button.setAttribute('aria-label', `Unmute ${name}`);
          list.append(item);
        }
        list.closest('[data-fc-muted]').hidden = !list.children.length;
      }

      document.addEventListener('click', async (event) => {
        const chip = event.target.closest('[data-mute-channel]');
        if (!chip) return;
        event.preventDefault();
        const muted = chip.classList.toggle('is-muted');
        chip.setAttribute('aria-pressed', muted ? 'true' : 'false');
        const key = chip.dataset.muteChannel;
        const name = chip.querySelector('span')?.textContent || key;
        try {
          /* One key at a time: saving the list of muted pills instead dropped every creator
             muted from a card menu, since those are not among the pills. */
          const response = await fetch('/api/feed/muted-channels', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ channel_key: key, muted })
          });
          if (!response.ok) throw new Error(`HTTP ${response.status}`);
          syncMutedList(key, name, muted);
          showToast(muted ? 'Creator muted' : 'Creator unmuted', { kind: 'success' });
        } catch (error) {
          chip.classList.toggle('is-muted', !muted);
          chip.setAttribute('aria-pressed', muted ? 'false' : 'true');
          showToast(`Could not save that mute: ${failureCause(error)}`, { kind: 'error' });
        }
      });

      document.addEventListener('click', async (event) => {
        const button = event.target.closest('[data-unmute-channel]');
        if (!button) return;
        event.preventDefault();
        const key = button.dataset.unmuteChannel;
        const name = button.parentElement?.querySelector('.fc-muted-name')?.textContent || 'Creator';
        button.disabled = true;
        try {
          const response = await fetch('/api/feed/muted-channels', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ channel_key: key, muted: false })
          });
          if (!response.ok) throw new Error(`HTTP ${response.status}`);
          document.querySelectorAll('[data-mute-channel]').forEach((chip) => {
            if (chip.dataset.muteChannel === key) {
              chip.classList.remove('is-muted');
              chip.setAttribute('aria-pressed', 'false');
            }
          });
          const row = button.closest('.fc-muted-row');
          const list = row?.parentElement;
          row?.remove();
          if (list && !list.children.length) list.closest('[data-fc-muted]').hidden = true;
          showToast(`${name} unmuted. Recommendations include them again.`, { kind: 'success' });
        } catch (error) {
          button.disabled = false;
          showToast(`Could not unmute: ${failureCause(error)}`, { kind: 'error' });
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
          if (input.value.trim()) { window.ofNavPending(); form.submit(); return; }
          form.classList.remove('is-open');
          toggle.setAttribute('aria-expanded', 'false');
          return;
        }
        form.classList.add('is-open');
        toggle.setAttribute('aria-expanded', 'true');
        input.focus();
      });
      document.addEventListener('submit', (event) => {
        const form = event.target.closest?.('[data-ch-search]');
        if (!form) return;
        const input = form.querySelector('[data-ch-search-input]');
        if (input.value.trim()) return;
        /* Enter in an empty box: clear an active search, otherwise do nothing. Never ?q=. */
        event.preventDefault();
        if (new URLSearchParams(location.search).has('q')) { window.ofNavPending(); location.href = form.getAttribute('action'); }
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

      /* ---- status: pane and page share one body ---- */
      (() => {
        const FRESH_MS = 30000;
        const fetchedAt = new WeakMap();
        let ticker = 0;

        function tickCooldowns() {
          const nodes = [...document.querySelectorAll('[data-cooldown]')];
          if (!nodes.length) { window.clearInterval(ticker); ticker = 0; return; }
          nodes.forEach((node) => {
            const left = Number(node.dataset.cooldown) - 1;
            node.dataset.cooldown = String(left);
            node.textContent = left > 0 ? `retrying in ${left}s` : 'retry due';
            if (left <= 0) node.removeAttribute('data-cooldown');
          });
        }

        /* The verdict is drawn before the walk finishes, and the walk is the better witness:
           a passing check can come from a cached probe. A broken hop takes over the headline
           and whatever it said moves into the line below. A failed database stays on top. */
        function reflectHops(body, diagnosis) {
          const verdict = body.querySelector('.status-verdict');
          const broken = (diagnosis.steps || []).find((hop) => !hop.ok);
          if (!verdict || !broken) return;
          const heading = verdict.querySelector('h2');
          const line = verdict.querySelector('p');
          const dot = verdict.querySelector('.status-dot');
          if (verdict.classList.contains('is-bad')) {
            line.textContent = `Private playback also breaks at ${broken.name}. ${line.textContent}`;
            return;
          }
          const earlier = verdict.classList.contains('is-warn') ? `${heading.textContent}. ` : '';
          heading.textContent = `Private playback is failing at ${broken.name}${broken.detail ? `: ${broken.detail}` : ''}`;
          line.textContent = `${earlier}${line.textContent}`;
          verdict.classList.replace('is-ok', 'is-warn');
          dot?.classList.replace('is-ok', 'is-warn');
        }

        async function runHops(body) {
          const box = body.querySelector('[data-status-hops]');
          const button = body.querySelector('[data-status-hops-run]');
          if (!box) return;
          if (button) button.disabled = true;
          try {
            const response = await fetch('/api/playback/diagnose');
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            const data = await response.json();
            renderDiagnosis(box, data.diagnosis || {}, data.hops || []);
            reflectHops(body, data.diagnosis || {});
          } catch (error) {
            const failed = document.createElement('p');
            failed.className = 'dialog-note';
            failed.textContent = `Could not run the hop walk: ${failureCause(error)}.`;
            box.replaceChildren(failed);
          } finally {
            if (button) button.disabled = false;
          }
        }

        function failure(body, message, retry) {
          const line = document.createElement('p');
          line.className = 'dialog-note';
          line.textContent = message;
          body.replaceChildren(line);
          if (!retry) return;
          const button = document.createElement('button');
          button.type = 'button';
          button.className = 'btn btn-sm';
          button.textContent = 'Try again';
          button.addEventListener('click', () => loadStatus(body, true));
          body.appendChild(button);
        }

        async function loadStatus(body, force = false) {
          if (body.getAttribute('aria-busy') === 'true') return;
          if (!force && Date.now() - (fetchedAt.get(body) || 0) < FRESH_MS) return;
          body.setAttribute('aria-busy', 'true');
          const refresh = body.querySelector('[data-status-refresh]');
          if (refresh) { refresh.disabled = true; refresh.textContent = 'Refreshing'; }
          try {
            const response = await fetch('/api/status/panel');
            if (response.status === 403) { failure(body, 'Status is limited to administrators.', false); return; }
            if (response.status === 401) { failure(body, 'Your session ended. Sign in again to see status.', false); return; }
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            body.innerHTML = await response.text();
            fetchedAt.set(body, Date.now());
            if ('statusAutoload' in body.dataset) body.querySelector('[data-status-page-link]')?.remove();
            if (!ticker) ticker = window.setInterval(tickCooldowns, 1000);
            runHops(body);
          } catch (error) {
            failure(body, `Could not load status: ${failureCause(error)}.`, true);
          } finally {
            body.removeAttribute('aria-busy');
          }
        }

        document.addEventListener('click', (event) => {
          if (event.target.closest('[data-status-refresh]')) {
            const body = event.target.closest('[data-status-body]');
            if (body) loadStatus(body, true);
            return;
          }
          if (event.target.closest('[data-status-hops-run]')) {
            const body = event.target.closest('[data-status-body]');
            if (body) runHops(body);
            return;
          }
          if (event.target.closest('[data-settings-pane="status"]')) {
            const body = document.querySelector('#settings-dialog [data-status-body]');
            if (body) loadStatus(body);
          }
        });
        document.querySelectorAll('[data-status-autoload]').forEach((body) => loadStatus(body, true));
      })();

      /* ---- first-run checklist ---- */
      document.addEventListener('click', async (event) => {
        const button = event.target.closest('[data-setup-dismiss]');
        if (!button) return;
        button.disabled = true;
        try {
          await savePreferences({ setup_dismissed: true });
          button.closest('[data-setup-card]')?.remove();
          showToast('Setup hidden. Everything on it is in Settings.', { kind: 'success' });
        } catch (error) {
          button.disabled = false;
          showToast(`Could not hide the setup list: ${failureCause(error)}`, { kind: 'error' });
        }
      });

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
          showToast('Could not refresh the feed', { kind: 'error' });
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
          const response = await fetch('/api/takeout/subscriptions/retry', { method: 'POST' });
          if (!response.ok) throw new Error(`HTTP ${response.status}`);
          const data = await response.json();
          showToast(`${data.cleared} channels queued to try again`, { kind: 'success' });
        } catch (error) {
          showToast(`Could not queue those channels: ${failureCause(error)}`, { kind: 'error' });
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
          showToast('Could not finish subscribing', { kind: 'error' });
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
              const response = await fetch(`/api/videos/${encodeURIComponent(detailsVideoId)}/details`);
              if (!response.ok) throw new Error('poll failed');
              const data = await response.json();
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
              /* A transient network error must not kill the poll; the attempt cap is
                 what bounds it. */
              return;
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

        /* A video that opens partway in says so under the player (the server renders the
           note), with a way back to the start: resuming silently looks like a bug the
           first time it happens to you. It stays out of the way until dismissed. */
        const resumeNote = document.querySelector('[data-resume-note]');
        resumeNote?.querySelector('[data-resume-dismiss]')?.addEventListener('click', () => {
          resumeNote.remove();
        });

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
          showToast('Could not update notifications', { kind: 'error' });
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
        const atEnd = track.scrollLeft >= max;
        if (prev) prev.disabled = track.scrollLeft <= 4;
        if (next) next.disabled = atEnd;
        shelf.classList.toggle('at-end', atEnd);
        /* A shelf that fits has nothing to scroll, so it does not offer to. */
        shelf.classList.toggle('fits', max <= 0);
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
          hideShelf(dismiss);
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

      /* ---- hidden shelves ----
         "Hide this shelf" is stored per user, so the shelf stays gone after a reload. Undo
         and Feed controls → Hidden shelves both take it back. The section is hidden rather
         than removed until then, so Undo can put it back in place. */
      async function setShelfHidden(key, hidden) {
        const response = await fetch('/api/feed/hidden-shelves', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ shelf_key: key, hidden })
        });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
      }

      function syncHiddenShelfList(key, title, hidden) {
        const box = document.querySelector('[data-fc-hidden-shelves]');
        const list = box?.querySelector('[data-fc-hidden-list]');
        if (!list) return;
        list.querySelectorAll('[data-hidden-shelf-row]').forEach((row) => {
          if (row.dataset.hiddenShelfRow === key) row.remove();
        });
        if (hidden) {
          const row = document.createElement('li');
          row.className = 'fc-muted-row';
          row.dataset.hiddenShelfRow = key;
          row.innerHTML = '<span class="fc-muted-name"></span><button class="btn btn-sm" type="button">Show</button>';
          row.querySelector('span').textContent = title;
          const button = row.querySelector('button');
          button.dataset.showShelf = key;
          button.setAttribute('aria-label', `Show ${title} again`);
          list.append(row);
        }
        box.hidden = !list.children.length;
      }

      async function hideShelf(button) {
        const key = button.dataset.dismissShelf;
        const title = button.dataset.shelfTitle || 'Shelf';
        const section = button.closest('[data-feed-section]');
        if (!key || !section) return;
        const next = section.nextElementSibling;
        section.hidden = true;
        next?.querySelector('a[href], button:not(:disabled)')?.focus({ preventScroll: true });
        try {
          await setShelfHidden(key, true);
        } catch (error) {
          section.hidden = false;
          showToast(`Could not hide ${title}: ${failureCause(error)}`, { kind: 'error' });
          return;
        }
        syncHiddenShelfList(key, title, true);
        showToast(`${title} hidden. Feed controls can show it again.`, {
          label: 'Undo',
          onClick: async () => {
            try {
              await setShelfHidden(key, false);
            } catch (error) {
              showToast(`Could not undo: ${failureCause(error)}`, { kind: 'error' });
              return;
            }
            section.hidden = false;
            syncHiddenShelfList(key, title, false);
            section.querySelector('[data-dismiss-shelf]')?.focus({ preventScroll: true });
          }
        });
      }

      document.addEventListener('click', async (event) => {
        const button = event.target.closest('[data-show-shelf]');
        if (!button) return;
        const key = button.dataset.showShelf;
        const title = button.closest('[data-hidden-shelf-row]')?.querySelector('.fc-muted-name')?.textContent || 'Shelf';
        button.disabled = true;
        try {
          await setShelfHidden(key, false);
        } catch (error) {
          button.disabled = false;
          showToast(`Could not show ${title}: ${failureCause(error)}`, { kind: 'error' });
          return;
        }
        /* A shelf hidden on this visit is still in the page; any other returns on the next load. */
        const dismissed = [...document.querySelectorAll('[data-dismiss-shelf]')].find((item) => item.dataset.dismissShelf === key);
        const section = dismissed?.closest('[data-feed-section]');
        const box = button.closest('[data-fc-hidden-shelves]');
        syncHiddenShelfList(key, title, false);
        box?.querySelector('[data-show-shelf]')?.focus({ preventScroll: true });
        if (section) {
          section.hidden = false;
          showToast(`${title} is back on Home`, { kind: 'success' });
        } else {
          showToast(`${title} shows again the next time Home loads`, { kind: 'success', label: 'Reload now', onClick: () => window.location.reload() });
        }
      });

      /* ---- client-side filtering on list pages ---- */
      document.querySelectorAll('[data-filter-input]').forEach((input) => {
        input.addEventListener('input', () => {
          const needle = input.value.trim().toLowerCase();
          /* Timeline cards have no filter label of their own, so they match on their text. */
          const items = '[data-filter-item], [data-filter-group] [data-video-card]';
          document.querySelectorAll(items).forEach((item) => {
            const haystack = (item.dataset.filterItem || item.textContent || '').toLowerCase();
            item.hidden = Boolean(needle) && !haystack.includes(needle);
          });
          document.querySelectorAll('[data-filter-group]').forEach((group) => {
            group.hidden = !group.querySelector('[data-video-card]:not([hidden])');
          });
          const none = document.querySelector('[data-filter-empty]');
          if (none) none.hidden = !needle || Boolean(document.querySelector(items.split(', ').map((part) => `${part}:not([hidden])`).join(', ')));
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
            <button class="icon-button nav-toggle" type="button" data-nav-toggle aria-label="Toggle navigation" aria-expanded="true">{_icon("menu")}</button>
            <a class="brand" href="/" aria-label="OpenFeed home">{_brand_mark()}<span class="brand-word">Open<b>Feed</b></span></a>
          </div>
          <div class="header-center">
            <form class="search-form" action="/search" method="get" role="search">
              <input name="q" type="search" value="{escape(search_value, quote=True)}" placeholder="Search" aria-label="Search" autocomplete="off">
              <kbd class="search-hint" aria-hidden="true">/</kbd>
              <button class="search-clear" type="button" data-search-clear aria-label="Clear search">{_icon("x")}</button>
              <button class="search-submit" type="submit" aria-label="Search">{_icon("search")}</button>
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


def _app_sidebar(active: str, user_key: str = "") -> str:
    return f'''
        <aside class="desktop-sidebar" aria-label="Primary navigation">
          {_home_nav(active, user_key)}
          <div class="sidebar-meta">{_source_offer()}</div>
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


def _page_head(title: str, meta: str = "", actions: str = "") -> str:
    """The one-line page header: title, count or meta, actions right. A .toolbar may follow.

    title and meta are plain text and are escaped here. actions is trusted HTML.
    """
    meta_html = f'<span class="page-meta">{escape(meta)}</span>' if meta else ""
    actions_html = f'<div class="page-actions">{actions}</div>' if actions else ""
    return f'<header class="page-head"><h1>{escape(title)}</h1>{meta_html}{actions_html}</header>'


def _empty_state(icon: str, title: str, body: str, action_html: str = "") -> str:
    """Icon name, title, a line or two on how things get here, and at most one action.

    title and body are plain text and are escaped here. action_html is trusted HTML.
    """
    action = f'<div class="empty-state-action">{action_html}</div>' if action_html else ""
    return f'<div class="empty-state">{_icon(icon)}<h2>{escape(title)}</h2><p>{escape(body)}</p>{action}</div>'


def _skeleton_cards(count: int) -> str:
    """Placeholder cards for a .video-grid. They reuse the card classes, so the sizes match."""
    card = (
        '<div class="video-card" aria-hidden="true"><div class="thumb skeleton"></div>'
        '<div class="video-info"><span class="skeleton skeleton-avatar"></span><div class="card-copy">'
        '<span class="skeleton skeleton-line w-92"></span><span class="skeleton skeleton-line w-64"></span>'
        '<span class="skeleton skeleton-line is-meta w-40"></span></div></div></div>'
    )
    return card * max(0, count)


def _skeleton_rows(count: int) -> str:
    """Placeholder rows for a .result-list or .library-list, on the real .media-row grid."""
    row = (
        '<div class="media-row skeleton-row" aria-hidden="true"><div class="thumb skeleton"></div>'
        '<div class="media-row-copy"><span class="skeleton skeleton-line w-92"></span>'
        '<span class="skeleton skeleton-line is-meta w-64"></span>'
        '<span class="skeleton skeleton-line is-meta w-40"></span></div></div>'
    )
    return row * max(0, count)


def _feed_loading(label: str, attribute: str, placeholders: str) -> str:
    """The tail of a paginated list. The script toggles `hidden` on the data attribute."""
    return (
        f'<div class="feed-loading" {attribute} hidden role="status">'
        f'<span class="sr-only">{escape(label)}</span>{placeholders}</div>'
    )


def _segmented(name: str, options: list[tuple[str, str]], selected: str, label: str = "") -> str:
    """A radio group drawn as a pill row, the selected pill inverted. options: (value, label)."""
    aria = f' aria-label="{escape(label, quote=True)}"' if label else ""
    items = "".join(
        f'<label class="segment"><input class="sr-only" type="radio" name="{escape(name, quote=True)}" '
        f'value="{escape(value, quote=True)}"{" checked" if value == selected else ""}>{escape(text)}</label>'
        for value, text in options
    )
    return f'<div class="segmented" role="radiogroup"{aria}>{items}</div>'


def _toggle(name: str, label: str, checked: bool = False, note: str = "", attrs: str = "") -> str:
    """A labelled switch row around a real checkbox. attrs is trusted HTML."""
    note_html = f"<small>{escape(note)}</small>" if note else ""
    return (
        f'<label class="toggle-row"><span><strong>{escape(label)}</strong>{note_html}</span>'
        f'<input class="switch" type="checkbox" role="switch" name="{escape(name, quote=True)}"'
        f'{" checked" if checked else ""} {attrs}></label>'
    )


def _select(name: str, options: list[tuple[str, str]], selected: str = "", label: str = "", attrs: str = "") -> str:
    """The native select, restyled. options: (value, label). attrs is trusted HTML."""
    aria = f' aria-label="{escape(label, quote=True)}"' if label else ""
    items = "".join(
        f'<option value="{escape(value, quote=True)}"{" selected" if value == selected else ""}>{escape(text)}</option>'
        for value, text in options
    )
    return f'<select class="select" name="{escape(name, quote=True)}"{aria} {attrs}>{items}</select>'


def _ranker_mode_select(selected: str, attrs: str = "") -> str:
    """The recommendation mode picker, shared by Settings and the feed dialog."""
    from . import ranker

    options = [("", f"Automatic ({ranker.MODE_LABELS[ranker.DEFAULT_MODE]})")]
    options += [(mode, ranker.MODE_LABELS[mode]) for mode in ranker.MODES]
    return _select("ranker_mode", options, selected if selected in ranker.MODES else "", "Recommendation ranking", attrs)


def _ranker_mode_caption() -> str:
    from . import ranker

    return (
        "Graph ranks the whole library by how YouTube's related lists link it to the channels you follow and the "
        "videos you watched at least halfway or liked. Graph + popularity mixes that with views and age. Classic is the original topic "
        f"scorer. Automatic follows the shipped default, now {ranker.MODE_LABELS[ranker.DEFAULT_MODE]}. In Graph, "
        "how far to wander sets the share of new uploads from related creators, prioritizing subscriptions sets the "
        "share linked from your channels, followed topics get a mild boost and muted topics are left out. Muted "
        "creators apply everywhere."
    )


def _range_field(name: str, label: str, value: int, minimum: int = 0, maximum: int = 100, step: int = 1, suffix: str = "", attrs: str = "") -> str:
    """A slider with a live <output>, kept current by the shared script. attrs is trusted HTML."""
    safe_name = escape(name, quote=True)
    safe_suffix = escape(suffix, quote=True)
    field_id = f"range-{safe_name}"
    return (
        f'<div class="range-field"><label class="range-field-label" for="{field_id}">{escape(label)}</label>'
        f'<output for="{field_id}" class="range-output" data-suffix="{safe_suffix}">{int(value)}{safe_suffix}</output>'
        f'<input class="range" id="{field_id}" type="range" name="{safe_name}" min="{int(minimum)}" max="{int(maximum)}" '
        f'step="{int(step)}" value="{int(value)}" {attrs}></div>'
    )


def _shell_head(appearance_choice: str = "") -> str:
    """Everything the two page shells share in <head>, so they cannot drift apart.

    appearance_choice is the user's stored theme, when the page knows the user; the head
    script falls back to this browser's mirror of it, then to dark.
    """
    from . import appearance

    return (
        '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'
        f"{appearance.head(appearance_choice)}"
        f"{_head_icons()}"
        f'<link rel="stylesheet" href="/assets/shell/{_content_hash(_home_styles())}.css">'
        # In the head so a page that starts on the rail does not paint the full sidebar first.
        "<script>try{if(innerWidth<=1100||localStorage.getItem('openfeed-nav')==='collapsed')"
        "document.documentElement.classList.add('nav-rail')}catch(e){}</script>"
    )


def _shell_foot() -> str:
    """The toast region and the shell script.

    A plain script tag on purpose, in the slot the inline script used to fill. A page's
    own inline scripts follow it and call showToast and friends, and the deferred token
    script sits earlier in the document, so `defer` here would reorder both.
    """
    return (
        '<div class="toast-stack" data-toast-region popover="manual" aria-live="polite" aria-relevant="additions"></div>'
        f'<script src="/assets/shell/{_content_hash(_home_script())}.js"></script>'
    )


def _dearrow_body_attrs(user_key: str) -> str:
    """Tells the shell script to ask for DeArrow titles. Empty when DeArrow is off."""
    from . import dearrow

    try:
        prefs = dearrow.preferences(user_key)
    except Exception:
        return ""
    if not prefs["enabled"]:
        return ""
    casing = f' data-dearrow-casing="{escape(prefs["casing"], quote=True)}"' if prefs["casing"] else ""
    return f' data-dearrow="on"{casing}'


def _app_shell(title: str, active: str, content: str, search_value: str = "", main_class: str = "app-page", scripts: str = "", request: Request | None = None, immersive: bool = False) -> str:
    user = _current_user(request)
    user_key = _user_key(request)
    return f'''
    <!doctype html>
    <html lang="en">
      <head>
        <meta charset="utf-8">
        <title>{escape(title)} - OpenFeed</title>
        {_shell_head(_appearance_choice(user_key))}
      </head>
      <body{' class="immersive-nav"' if immersive else ''}{_dearrow_body_attrs(user_key)}>
        <a class="skip-link" href="#main">Skip to content</a>
        {_app_header(search_value, user, user_key)}
        {_app_sidebar(active, user_key)}
        <div class="nav-scrim" data-nav-scrim aria-hidden="true"></div>
        <main class="home-main {escape(main_class)}" id="main" tabindex="-1">{content}</main>
        <nav class="mobile-bottom-nav" aria-label="Mobile navigation">{_mobile_nav(active)}</nav>
        {_settings_dialog(user, user_key)}
        {_playlist_dialogs()}
        {_collection_dialogs()}
        {_shell_foot()}
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
    *,
    meta: list[str] | None = None,
    note: str = "",
    note_danger: bool = False,
    thumb_overlay: str = "",
    menu_rows: str = "",
    variant: str = "",
    filter_text: str = "",
    shorts_route: bool = False,
    lead: str = "",
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
    a raw-HTML slot and must only ever receive server-generated, pre-sanitized markup
    (the playlist remove control) — never anything derived from user input. The same
    goes for `thumb_overlay` and `menu_rows`.

    `variant="library"` is the fixed-anatomy row of the library pages: channel and meta
    share a line, `note` (plain text, escaped here) sits under them, and the actions
    move into one cluster. `meta` (plain text, escaped here) replaces views and age, and
    `filter_text` is what a page's filter box matches, so it never matches menu labels.
    `lead` (trusted HTML, library variant only) goes first in the row: the drag handle.
    """
    title_text = str(video.get("title") or "Untitled")
    title = escape(title_text)
    channel_text = str(video.get("channel_title") or video.get("author") or "Unknown channel")
    channel = escape(channel_text)
    video_id_raw = str(video.get("youtube_video_id") or "")
    video_id = escape(video_id_raw)
    if not video_id:
        watch_href = "#"
    elif shorts_route and db.is_short_video(video):
        watch_href = f"/shorts?start={video_id}"
    else:
        watch_href = f"/watch/{video_id}"
    channel_id = escape(str(video.get("youtube_channel_id") or ""))
    channel_href = f"/channel/{channel_id}" if channel_id else "/channels"
    thumbnail = escape(_thumbnail_for(video))
    thumb_img = f'<img src="{thumbnail}" alt="" loading="lazy" onerror="this.remove()">' if thumbnail else ""
    thumb_fallback = escape(_avatar_initials(title_text))
    duration = escape(_duration_text(video.get("duration_seconds")))
    duration_html = f'<span class="duration">{duration}</span>' if duration else ""
    progress_html = _progress_rail(video)
    source_label = escape(label or _source_label(video))
    if meta is None:
        meta_parts = _video_meta_parts(video)
        time_left = _time_left_text(video)
        if time_left:
            meta_parts = [time_left, *[part for part in meta_parts if not part.endswith(" views")]]
    else:
        meta_parts = meta
    meta_html = " &middot; ".join(escape(part) for part in meta_parts)
    original_url = escape(_safe_external_href(video.get("url")) or f"https://www.youtube.com/watch?v={video_id_raw}")
    invidious_url = escape(f"{settings.invidious_public_url}/watch?v={video_id_raw}") if video_id_raw and settings.invidious_public_url else ""
    watched = _is_watched(video)
    menu = _video_action_menu(video_id_raw, title_text, watch_href, original_url, invidious_url, menu_rows, channel_id=str(video.get("youtube_channel_id") or ""), channel_title=channel_text, watched=watched, reason=str(video.get("recommendation_reason") or ""))
    quick = _card_quick_actions(video_id_raw, title_text, watched)
    index_html = f'<span class="row-index">{int(index)}</span>' if index is not None else ""
    playlist_attr = (
        f' data-playlist-row="{escape(str(playlist_row_id), quote=True)}"'
        if playlist_row_id else ""
    )
    filter_attr = f' data-filter-item="{escape(filter_text, quote=True)}"' if filter_text else ""
    thumb_html = (
        f'<a class="thumb" href="{watch_href}" aria-label="Watch {title}{", watched" if watched else ""}"><span class="thumb-fallback">{thumb_fallback}</span>'
        f'{thumb_img}{duration_html}{thumb_overlay or progress_html}</a>'
    )
    if variant == "library":
        classes = "media-row library-row with-index" if index is not None else "media-row library-row"
        byline = f'<a href="{channel_href}">{channel}</a>' + (f'<span class="row-meta">{meta_html}</span>' if meta_html else "")
        note_class = ' class="row-note is-danger"' if note_danger else ' class="row-note"'
        note_html = f"<p{note_class}>{escape(note)}</p>" if note else ""
        return f'''
    <article class="{classes}"{playlist_attr}{filter_attr} data-video-card data-video-id="{video_id}" data-channel-id="{channel_id}">
      {lead}{index_html}{thumb_html}
      <div class="media-row-copy">
        <h2><a href="{watch_href}">{title}</a></h2>
        <p class="row-byline">{byline}</p>
        {note_html}
      </div>
      <div class="row-actions">{trailing}{menu}</div>
    </article>
    '''
    return f'''
    <article class="media-row{' is-watched' if watched else ''}"{playlist_attr}{filter_attr} data-video-card data-video-id="{video_id}" data-channel-id="{channel_id}">
      {index_html}{thumb_html}
      <div class="media-row-copy">
        <h2><a href="{watch_href}">{title}</a></h2>
        <p><a href="{channel_href}">{channel}</a></p>
        <p>{meta_html or source_label}</p>
      </div>
      {menu}{trailing}{quick}
    </article>
    '''


def _row_remove(attrs: str, label: str) -> str:
    """The row-level remove control. attrs is trusted HTML; label is plain text."""
    text = escape(label, quote=True)
    return f'<button class="btn btn-ghost btn-icon row-remove" type="button" {attrs} aria-label="{text}" title="{text}">{_icon("x")}</button>'


def _count_text(count: int, noun: str = "video") -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def _runtime_text(seconds: Any, known: Any = None, count: Any = None) -> str:
    """A total running time such as 3h 12m, or "" when no duration is known.

    The durations come from upstream and some videos lack one. Given how many of `count`
    were `known`, a partial total says so with a plus rather than posing as the whole.
    """
    total = int(seconds or 0)
    if total <= 0:
        return ""
    hours, remainder = divmod(total, 3600)
    minutes = remainder // 60
    text = f"{hours}h {minutes}m" if hours else f"{max(1, minutes)}m"
    return f"{text}+" if known is not None and count is not None and int(known) < int(count) else text


def _library_filter(placeholder: str, extra: str = "") -> str:
    """The filter box for a list page. Pair with data-filter-item rows and _filter_empty.

    extra (trusted HTML) sits at the toolbar's end: a list's Sort and Select.
    """
    return (
        f'<div class="toolbar library-toolbar"><label class="toolbar-search">{_icon("search")}'
        f'<input type="search" placeholder="{escape(placeholder, quote=True)}" aria-label="{escape(placeholder, quote=True)}" '
        f'autocomplete="off" data-filter-input></label>{extra}</div>'
    )


def _filter_empty(noun: str) -> str:
    """The no-match state for a filter box. The script fills in what was typed."""
    return (
        f'<div class="library-filter-empty" data-filter-empty="{escape(noun, quote=True)}" hidden>'
        + _empty_state(
            "search", "No matches", "The filter checks titles and channels on this page.",
            '<button class="btn btn-sm" type="button" data-filter-clear>Clear filter</button>',
        )
        + "</div>"
    )


def _subscription_posts_shelf(user_key: str = "local", limit: int = 6) -> str:
    """Recent community posts from subscribed channels, for the feeds.

    Bounded hard: each channel is one upstream request, so this reads a slice of the
    subscription list rather than all of it. A page render must not turn into seventy
    calls to Invidious.
    """
    from . import posts as posts_module

    try:
        subscriptions = db.list_subscriptions(200, user_key=user_key)
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
        meta = " &middot; ".join(part for part in (_count_label(escape(views), "view") if views else "", escape(attachment.get("author") or "")) if part)
        return (
            f'<a class="post-video" href="/watch/{video_id}">'
            f'<span class="post-video-thumb"><img src="/vi/{video_id}/hqdefault.jpg" alt="" loading="lazy">'
            f'{f"<span class=\'duration\'>{duration}</span>" if duration else ""}</span>'
            f'<span class="post-video-copy"><strong>{title}</strong><small>{meta}</small></span></a>'
        )

    if kind == "image":
        # Post images live on yt3.ggpht.com: proxied, or left out.
        images = [escape(local, quote=True) for local in (privacy.same_origin_image(url) for url in (attachment.get("images") or [])) if local]
        cells = "".join(f'<img src="{url}" alt="" loading="lazy" onerror="this.remove()">' for url in images)
        multi = " is-multi" if len(images) > 1 else ""
        return f'<div class="post-images{multi}">{cells}</div>'

    if kind == "playlist":
        return (
            f'<a class="post-playlist" href="https://www.youtube.com/playlist?list={escape(str(attachment.get("playlist_id")), quote=True)}"'
            f' target="_blank" rel="noopener noreferrer">{_icon("playlist")}'
            f'<span><strong>{escape(str(attachment.get("title") or "Playlist"))}</strong>'
            f'<small>{_count_label(escape(str(attachment.get("video_count") or "")), "video")}</small></span></a>'
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
                f'<span class="poll-text">{escape(str(choice))}</span>'
                f'<span class="poll-mark">{_icon("check")}</span></button>'
            )
        cleared = "" if vote is None else (
            f'<button class="link-action" type="button" data-poll-clear="{escape(post_id, quote=True)}">Clear my answer</button>'
        )
        return (
            f'<div class="post-poll" data-poll="{escape(post_id, quote=True)}">'
            f'<p class="poll-label">Simulated locally</p>{"".join(rows)}'
            f'<p class="poll-note">{_icon("block")}<span><strong>Your answer stays here.</strong> '
            f'YouTube does not publish how a poll was split and does not accept votes without a signed-in '
            f'account, so this records what you picked locally and changes nothing on YouTube. '
            f'{f"{escape(total)} people voted there." if total else ""} {cleared}</span></p></div>'
        )

    return (
        f'<p class="post-unsupported">{_icon("hide")}<span>This post has an '
        f'{escape(str(attachment.get("raw_type") or "unknown"))} attachment that OpenFeed cannot show yet. '
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
    # content_html arrives verbatim from YouTube via Invidious with no sanitization pass
    # anywhere, so it is never trusted as markup: render the extracted plain text.
    body = escape(str(post.get("content") or post.get("content_html") or "")).replace("\n", "<br>")
    detail = f"/channel/{escape(channel_id, quote=True)}/posts/{escape(post_id, quote=True)}"
    meta = " &middot; ".join(part for part in (
        f'{likes} likes' if likes else "", f'{replies} replies' if replies else "") if part)
    return f'''
      <article class="post-card{" is-compact" if compact else ""}" data-post-id="{escape(post_id, quote=True)}">
        <header class="post-head">
          {avatar}
          <div><strong>{escape(author)}</strong><small>{when}{edited}</small></div>
          <a class="post-permalink" href="{detail}" aria-label="Open this post" title="Open this post">{_icon("chevron-right")}</a>
        </header>
        <div class="post-body">{body}</div>
        {_post_attachment_html(post.get("attachment"), post_id, vote)}
        {f'<p class="post-meta">{meta}</p>' if meta else ""}
      </article>
    '''


def _channel_card(channel: dict[str, Any], notify_level: str = "all") -> str:
    """One channel as a search result: 56px avatar, one meta line, subscribe state.

    `data-result-id` is what the infinite scroll reads to drop repeats.
    """
    title_text = str(channel.get("title") or "Unknown channel")
    channel_id_raw = str(channel.get("youtube_channel_id") or "")
    channel_id = escape(channel_id_raw, quote=True)
    href = f"/channel/{channel_id}" if channel_id else "/channels"
    parts: list[str] = []
    handle = str(channel.get("handle") or "").strip()
    if handle:
        parts.append(f'@{handle.lstrip("@")}')
    subscribers = _compact_count(channel.get("subscriber_count"))
    if subscribers:
        parts.append(_count_label(subscribers, "subscriber"))
    count = int(channel.get("video_count") or 0)
    if count:
        parts.append(f"{count} video{'' if count == 1 else 's'} here")
    latest = _relative_time(channel.get("latest_video_at"))
    if latest:
        parts.append(f"latest {latest}")
    meta_html = f"<p>{escape(' · '.join(parts))}</p>" if parts else ""
    verified = _icon("verified").replace('class="icon"', 'class="icon verified"') if channel.get("verified") else ""
    feed_url = str(channel.get("feed_url") or (_youtube_feed_url(channel_id_raw) if channel_id_raw else ""))
    control = _subscribe_control(channel_id_raw, feed_url, title_text, bool(channel.get("subscription_id")), notify_level)
    return f'''
    <article class="channel-result" data-result-id="{channel_id}" data-channel-id="{channel_id}">
      <a class="channel-result-main" href="{href}">
        {_avatar_html(title_text, channel.get("avatar_url") or channel.get("channel_avatar_url"))}
        <div class="channel-result-copy"><h2><span>{escape(title_text)}</span>{verified}</h2>{meta_html}</div>
      </a>
      {control}
    </article>
    '''


def _skeleton_channel_rows(count: int) -> str:
    """Placeholder rows for a channel result list, on the real .channel-result grid."""
    row = (
        '<div class="channel-result" aria-hidden="true"><div class="channel-result-main">'
        '<span class="skeleton skeleton-avatar channel-result-skeleton"></span><div class="channel-result-copy">'
        '<span class="skeleton skeleton-line w-40"></span><span class="skeleton skeleton-line is-meta w-64"></span>'
        '</div></div></div>'
    )
    return row * max(0, count)


def _filter_menu(label: str, options: list[tuple[str, str]], selected: str, href_for: Any) -> str:
    """A pill that opens a menu of links, so a filter is a page load and needs no script.

    options: (value, label), default first. A filter away from its default inverts, the
    same way a selected chip does. href_for maps a value to a raw URL; it is escaped here.
    """
    current = next((text for value, text in options if value == selected), options[0][1])
    changed = selected != options[0][0]
    rows = "".join(
        _menu_row(
            "check", text, href=escape(href_for(value), quote=True),
            trailing="\u2713" if value == selected else "",
            attrs=' aria-current="true"' if value == selected else "",
        )
        for value, text in options
    )
    return (
        f'<details class="filter-menu yt-menu">'
        f'<summary class="ch-chip filter-chip{" active" if changed else ""}" aria-label="{escape(label, quote=True)}: {escape(current, quote=True)}">'
        f'<span>{escape(current if changed else label)}</span>{_icon("chevron-down")}</summary>'
        f'<div class="menu-panel" role="menu" aria-label="{escape(label, quote=True)}">{rows}</div></details>'
    )


def _is_playlist_subscription(subscription: dict[str, Any]) -> bool:
    haystack = " ".join(str(subscription.get(key) or "") for key in ("id", "title", "feed_url", "html_url"))
    return "playlist" in haystack.lower() or "playlist_id=" in haystack.lower()


def _collection_chips(collections: list[tuple[str, str]]) -> str:
    """The collections a channel sits in: two named, the rest counted. (id, name) pairs."""
    chips = "".join(
        f'<a class="collection-chip" href="/collection/{escape(item_id, quote=True)}">{escape(name)}</a>'
        for item_id, name in collections[:2]
    )
    rest = [name for _, name in collections[2:]]
    if rest:
        chips += f'<span class="collection-chip is-more" title="{escape(", ".join(rest), quote=True)}">+{len(rest)}</span>'
    return f'<div class="channel-row-chips">{chips}</div>' if chips else ""


def _subscription_card(
    subscription: dict[str, Any],
    member_id: str | None = None,
    trailing: str = "",
    notify_level: str | None = None,
    collections: list[tuple[str, str]] | None = None,
) -> str:
    """One subscription as a dense manager row.

    `member_id` and `trailing` exist so collection_page can extend the row without
    running str.replace over the markup this function returns — the same string
    surgery _media_row grew out of. `member_id` is escaped inside this function;
    pass the raw id. `trailing` is the one raw-HTML slot and must only ever receive
    server-generated, pre-sanitized markup (the collection remove control) — never
    anything derived from user input.

    `notify_level` draws the bell only when the caller read the real setting: no value
    means no bell, rather than a bell claiming the default. `collections` is (id, name).
    """
    title_text = str(subscription.get("title") or "Untitled subscription")
    title = escape(title_text)
    channel_id_raw = str(subscription.get("youtube_channel_id") or "")
    channel_id = escape(channel_id_raw)
    external_url = _safe_external_href(subscription.get("html_url") or subscription.get("feed_url"))
    href = f"/channel/{channel_id}" if channel_id else (escape(external_url, quote=True) if external_url else "/channels")
    rel = ' rel="noreferrer"' if href.startswith(("http://", "https://")) else ""
    count = int(subscription.get("video_count") or 0)
    latest = _relative_time(subscription.get("latest_video_at"))
    parts = [f"Latest {latest}" if latest else "No dated videos yet", f"{count} video{'' if count == 1 else 's'}"]
    if not count and not latest and subscription.get("sort_note"):
        parts = []  # the note ("No YouTube videos in this feed") already says it
    if _is_playlist_subscription(subscription):
        parts.insert(0, "Playlist")
    if subscription.get("sort_note"):
        parts.insert(0, str(subscription["sort_note"]))
    subscription_id_raw = str(subscription.get("id") or "")
    subscription_id = escape(subscription_id_raw, quote=True)
    feed_url_raw = str(subscription.get("feed_url") or "")
    title_attr = escape(title_text, quote=True)
    rows = []
    if subscription_id_raw:
        rows.append(_menu_row("collections", "Add to collection", attrs=f' data-channel-collections="{subscription_id}" data-channel-title="{title_attr}"'))
    if external_url:
        rows.append(_menu_row("external", "Open on YouTube", href=escape(external_url, quote=True)))
    if channel_id_raw and feed_url_raw:
        rows.append(_menu_row(
            "x", "Unsubscribe", danger=True,
            attrs=f' data-unsubscribe-feed data-channel-id="{escape(channel_id_raw, quote=True)}" data-feed-url="{escape(feed_url_raw, quote=True)}" data-title="{title_attr}"',
        ))
    menu = (
        f'<details class="card-menu yt-menu"><summary class="menu-trigger" aria-label="Actions for {title_attr}">{_icon("more")}</summary>'
        f'<div class="menu-panel" role="menu" aria-label="Channel actions">{"".join(rows)}</div></details>'
    ) if rows else ""
    bell = _notification_bell(str(subscription.get("notify_key") or channel_id_raw), notify_level) if notify_level is not None else ""
    member_attr = (
        f' data-member-card="{escape(str(member_id), quote=True)}"'
        if member_id else ""
    )
    names = " ".join(name for _, name in collections or [])
    filter_text = f"{title_text} {names}".strip()
    return f'''
    <article class="channel-row"{member_attr} data-subscription-card="{subscription_id}" data-channel-id="{escape(channel_id_raw, quote=True)}" data-filter-item="{escape(filter_text, quote=True)}">
      <a class="channel-row-main" href="{href}"{rel}>
        {_avatar_html(title_text, subscription.get("channel_avatar_url"))}
        <div class="channel-row-copy"><h2>{title}</h2><p>{escape(" · ".join(parts))}</p></div>
      </a>
      {_collection_chips(collections or [])}
      <div class="channel-row-actions">{bell}{menu}{trailing}</div>
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
    name_attr = escape(name_text, quote=True)
    collection_id = escape(str(collection.get("id") or ""), quote=True)
    description_text = str(collection.get("description") or "")
    count = int(collection.get("subscription_count") or 0)
    latest = _relative_time(collection.get("latest_member_at"))
    meta = f"{count} channel{'' if count == 1 else 's'}"
    if latest:
        meta += f" · updated {latest}"
    href = f"/collection/{collection_id}"
    action = (
        f'<a class="btn btn-sm" href="{href}">{_icon("play")}<span>Open feed</span></a>'
        if count else
        f'<button class="btn btn-sm" type="button" data-add-channels="{collection_id}" data-collection-name="{name_attr}">{_icon("plus")}<span>Add channels</span></button>'
    )
    return f'''
    <article class="collection-card" data-collection-card="{collection_id}" data-filter-item="{name_attr}">
      <a class="collection-card-cover" href="{href}" aria-label="Open {name_attr}">{_collection_cover(collection)}</a>
      <div class="collection-card-body">
        <div>
          <h3><a href="{href}">{name}</a></h3>
          <p>{escape(meta)}</p>
        </div>
        <details class="card-menu yt-menu">
          <summary class="menu-trigger" aria-label="Collection actions for {name_attr}">{_icon("more")}</summary>
          <div class="menu-panel" role="menu" aria-label="Collection actions">
            {_menu_row("chevron-right", "Open collection", href=href)}
            {_menu_row("plus", "Add channels", attrs=f' data-add-channels="{collection_id}" data-collection-name="{name_attr}"')}
            {_menu_row("pencil", "Rename", attrs=f' data-edit-collection="{collection_id}" data-collection-name="{name_attr}" data-collection-description="{escape(description_text, quote=True)}"')}
            {_menu_row("trash", "Delete collection", attrs=f' data-delete-collection="{collection_id}"', danger=True)}
          </div>
        </details>
      </div>
      <div class="collection-card-actions">{action}</div>
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
            <button class="btn" type="button" data-close-dialog>Cancel</button>
            <button class="btn btn-primary" type="submit" data-collection-submit>Create</button>
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
          <button class="btn btn-primary" type="button" data-close-dialog>Done</button>
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
            <button class="btn btn-primary" type="submit">Create &amp; add</button>
          </form>
        </div>
      </dialog>
    '''


@app.get("/collection/{collection_id}", response_class=HTMLResponse)
def collection_page(request: Request, collection_id: str, limit: int = 36, view: str = ""):
    auth_redirect = _login_required_redirect(request)
    if auth_redirect:
        return auth_redirect
    if not _valid_uuid(collection_id):
        return _page_not_found(request, "Collection not found", "It may have been deleted, or the link is wrong.", "/channels?tab=collections", "All collections", "collections", "channels")
    settings = get_settings()
    user_key = _user_key(request)
    collection = db.get_subscription_collection(collection_id, user_key)
    if not collection:
        return _page_not_found(request, "Collection not found", "It may have been deleted, or the link is wrong.", "/channels?tab=collections", "All collections", "collections", "channels")

    members = db.list_collection_subscriptions(collection_id, user_key)
    name_text = str(collection.get("name") or "Untitled collection")
    collection_id_attr = escape(str(collection.get("id") or ""), quote=True)
    name_attr = escape(name_text, quote=True)
    description_text = str(collection.get("description") or "")
    member_count = len(members)
    latest = _relative_time(collection.get("latest_member_at"))
    meta = f'<span data-member-count>{member_count}</span> channel{"" if member_count == 1 else "s"}'
    if latest:
        meta += f" &middot; updated {escape(latest)}"
    description_html = f'<p class="collection-hero-description">{escape(description_text)}</p>' if description_text else ""
    add_button = (
        f'<button class="btn btn-primary" type="button" data-add-channels="{collection_id_attr}" data-collection-name="{name_attr}">'
        f'{_icon("plus")}<span>Add channels</span></button>'
    )
    view_key = "channels" if view == "channels" and members else "feed"
    href = f"/collection/{collection_id_attr}"

    if not members:
        body = _empty_state(
            "collections", "No channels in this collection",
            "Add channels you subscribe to. Their uploads then show up here as one feed.",
        )
    elif view_key == "channels":
        levels = _notification_levels(user_key)
        member_rows = []
        for member in members:
            member_id_raw = str(member.get("id") or "")
            title_text = str(member.get("title") or "Untitled subscription")
            remove = (
                f'<button class="btn btn-ghost btn-icon collection-remove" type="button" data-remove-collection-member="{escape(member_id_raw, quote=True)}" '
                f'data-collection-id="{collection_id_attr}" aria-label="Remove {escape(title_text, quote=True)} from {name_attr}" '
                f'title="Remove from collection">{_icon("minus-circle")}</button>'
            )
            channel_key = str(member.get("youtube_channel_id") or "")
            member_rows.append(_subscription_card(
                member, member_id=member_id_raw or None, trailing=remove,
                notify_level=levels.get(channel_key, "all") if channel_key else None,
            ))
        body = f'''
          <div class="channel-grid collection-members">{"".join(member_rows)}</div>
          <div data-filter-empty hidden>{_empty_state("search", "No channels match", "Nothing in this collection matches that filter.")}</div>
        '''
    else:
        videos = db.list_collection_videos(collection_id, max(1, min(limit, 120)), user_key)
        cards = "".join(_home_video_card(video, index, settings) for index, video in enumerate(videos, start=1))
        body = f'<h2 class="sr-only">Latest videos</h2><div class="video-grid">{cards}</div>' if cards else _empty_state(
            "play", "No videos from these channels yet",
            "Uploads appear here after the next sync.",
            '<button class="btn" type="button" data-feed-refresh>Sync now</button>',
        )

    toolbar = ""
    if members:
        segments = "".join(
            f'<a class="segment" href="{target}"{" aria-current=\"page\"" if key == view_key else ""}>{label}</a>'
            for key, label, target in (("feed", "Feed", href), ("channels", "Channels", f"{href}?view=channels"))
        )
        filter_html = (
            f'<label class="toolbar-search">{_icon("search")}<input type="search" placeholder="Filter channels" '
            f'data-filter-input aria-label="Filter channels"></label>'
            if view_key == "channels" else ""
        )
        toolbar = f'<div class="toolbar subs-toolbar"><nav class="segmented" aria-label="Collection view">{segments}</nav>{filter_html}</div>'

    content = f'''
      <section class="collection-hero">
        <div class="collection-hero-cover">{_collection_cover({
            "member_titles": [m.get("title") for m in members[:4]],
            "member_avatars": [m.get("channel_avatar_url") or "" for m in members[:4]],
        })}</div>
        <div class="collection-hero-copy">
          <h1>{escape(name_text)}</h1>
          <p class="collection-hero-meta">{meta}</p>
          {description_html}
          <div class="collection-hero-actions">
            {add_button}
            <button class="btn" type="button" data-edit-collection="{collection_id_attr}" data-collection-name="{name_attr}" data-collection-description="{escape(description_text, quote=True)}">{_icon("pencil")}<span>Rename</span></button>
            <button class="btn btn-danger" type="button" data-delete-collection="{collection_id_attr}" data-redirect="/channels?tab=collections">{_icon("trash")}<span>Delete</span></button>
          </div>
        </div>
      </section>
      {toolbar}
      {body}
    '''
    return _app_shell(name_text, "collections", content, request=request)


def _cover_media(cover: str, count: int, icon: str = "playlist") -> str:
    """What goes inside .playlist-cover. cover is an escaped URL.

    The designed empty state sits underneath: it shows for a playlist with no videos, and
    it is also what is left behind when a cover image fails and removes itself.
    """
    image = f'<img src="{cover}" alt="" loading="lazy" onerror="this.remove()">' if cover else ""
    label = "<span>No videos yet</span>" if not count else ""
    pill = (
        f'<span class="playlist-count">{_icon(icon)}<span>{count}</span></span>'
        if count else ""
    )
    return f'<span class="playlist-cover-empty">{_icon(icon)}{label}</span>{image}{pill}'


def _playlist_card(playlist: dict[str, Any], owner_view: bool = True) -> str:
    """A playlist tile. owner_view=False is someone else's public playlist: no edit menu."""
    name_text = str(playlist.get("name") or "Untitled playlist")
    name = escape(name_text)
    playlist_id = escape(str(playlist.get("id") or ""), quote=True)
    description_text = str(playlist.get("description") or "")
    count = int(playlist.get("video_count") or 0)
    runtime = _runtime_text(playlist.get("runtime_seconds"), playlist.get("runtime_known"), count)
    visibility = library_tools.clean_visibility(playlist.get("visibility"))
    if owner_view:
        meta = " · ".join(part for part in (_count_text(count), runtime, library_tools.visibility_label(visibility)) if part)
    else:
        owner = str(playlist.get("owner_name") or "")
        meta = " · ".join(part for part in (f"by {owner}" if owner else "", _count_text(count)) if part)
    cover = escape(_thumbnail_for({
        "youtube_video_id": playlist.get("cover_youtube_video_id"),
        "thumbnail_url": playlist.get("cover_thumbnail_url"),
    }))
    href = f"/playlist/{playlist_id}"
    menu = f'''
        <details class="card-menu yt-menu">
          <summary class="menu-trigger" aria-label="Playlist actions for {name}">{_icon("more")}</summary>
          <div class="menu-panel" role="menu" aria-label="Playlist actions">
            {_menu_row("play", "Open playlist", href=href)}
            {_menu_row("pencil", "Edit", attrs=f' data-edit-playlist="{playlist_id}" data-playlist-name="{escape(name_text, quote=True)}" data-playlist-description="{escape(description_text, quote=True)}" data-playlist-visibility="{visibility}"')}
            {_menu_row("trash", "Delete playlist", attrs=f' data-delete-playlist="{playlist_id}" data-playlist-name="{escape(name_text, quote=True)}"', danger=True)}
          </div>
        </details>''' if owner_view else ""
    return f'''
    <article class="playlist-card" data-playlist-card="{playlist_id}" data-filter-item="{escape(name_text, quote=True)}">
      <div class="playlist-stack">
        <a class="playlist-cover" href="{href}" aria-label="Open {name}">{_cover_media(cover, count)}</a>
      </div>
      <div class="playlist-body">
        <div>
          <h3><a href="{href}">{name}</a></h3>
          <p>{escape(meta)}</p>
        </div>{menu}
      </div>
    </article>
    '''


def _smart_list_card(name: str, href: str, icon: str, count: int, meta: str, cover_video: dict[str, Any] | None) -> str:
    """A list the app keeps by itself. Same tile as a playlist, minus rename and delete."""
    cover = escape(_thumbnail_for(cover_video)) if cover_video else ""
    return f'''
    <article class="playlist-card is-smart" data-filter-item="{escape(name, quote=True)}">
      <div class="playlist-stack">
        <a class="playlist-cover" href="{escape(href, quote=True)}" aria-label="Open {escape(name, quote=True)}">{_cover_media(cover, count, icon)}</a>
      </div>
      <div class="playlist-body">
        <div>
          <h3><a href="{escape(href, quote=True)}">{escape(name)}</a></h3>
          <p>{escape(meta)}</p>
        </div>
      </div>
    </article>
    '''


def _new_playlist_tile() -> str:
    return f'''
    <div class="playlist-card">
      <div class="playlist-stack">
        <button class="playlist-cover playlist-cover-new" type="button" data-new-playlist>{_icon("plus")}<span>New playlist</span></button>
      </div>
    </div>
    '''


def _playlist_dialogs() -> str:
    visibility_options = "".join(
        f'<option value="{key}">{escape(label)} – {escape(sub)}</option>' for key, label, sub in library_tools.VISIBILITIES
    )
    return f'''
      <dialog class="feed-dialog playlist-edit-dialog" id="playlist-edit-dialog" aria-labelledby="playlist-edit-title">
        <form id="playlist-edit-form">
          <div class="dialog-head">
            <div>
              <h2 id="playlist-edit-title">New playlist</h2>
              <p>Private unless you choose otherwise. Nothing is ever shared outside this OpenFeed.</p>
            </div>
            <button class="icon-button" type="button" data-close-dialog aria-label="Close">{_icon("x")}</button>
          </div>
          <div class="dialog-body">
            <label class="text-field">Name<input name="name" type="text" required maxlength="120" autocomplete="off"></label>
            <label class="text-field">Description<input name="description" type="text" placeholder="Optional" maxlength="400" autocomplete="off"></label>
            <label class="text-field">Visibility<select class="field-input" name="visibility">{visibility_options}</select></label>
            <p class="dialog-alt" data-playlist-import-alt>Have one on YouTube? <button class="link-button" type="button" data-open-playlist-import>Import it by link</button></p>
          </div>
          <div class="dialog-actions">
            <p class="status-line" data-playlist-status role="status"></p>
            <button class="btn btn-ghost" type="button" data-close-dialog>Cancel</button>
            <button class="btn btn-primary" type="submit" data-playlist-submit>Create</button>
          </div>
        </form>
      </dialog>
      <dialog class="feed-dialog save-picker" id="playlist-picker-dialog" aria-labelledby="playlist-picker-title">
        <div class="dialog-head">
          <div>
            <h2 id="playlist-picker-title">Save to playlist</h2>
            <p class="save-picker-subtitle" data-picker-subtitle></p>
          </div>
          <button class="icon-button" type="button" data-close-dialog aria-label="Close">{_icon("x")}</button>
        </div>
        <div class="save-picker-search">
          <input class="field-input" type="search" placeholder="Find or create a playlist" aria-label="Find or create a playlist" maxlength="120" autocomplete="off" data-picker-filter>
        </div>
        <div class="save-picker-list" data-picker-list></div>
      </dialog>
      {_playlist_import_dialog()}
    '''


@app.get("/", response_class=HTMLResponse)
def index(request: Request, filter: str = "all"):
    auth_redirect = _login_required_redirect(request)
    if auth_redirect:
        return auth_redirect
    settings = get_settings()
    user = _current_user(request)
    user_key = str(user.get("user_key") or "local")
    recommendations_preview = db.list_recommended_videos(40, 0, "all", user_key)
    library_videos = db.list_subscription_recommendations(24, user_key)
    all_for_channels = recommendations_preview + library_videos
    # ?filter= makes a chip linkable, which is what gives the Continue watching shelf a
    # real "View all" and lets a reload keep the filter.
    feed_filter = _home_filter_key(filter)
    initial_sections = _home_feed_sections_html(0, feed_filter, settings, user_key)
    if not initial_sections.strip():
        initial_sections = _home_empty_state(feed_filter)
    creator_strip = _creator_strip(all_for_channels)

    return f'''
    <!doctype html>
    <html lang="en">
      <head>
        <meta charset="utf-8">
        <title>OpenFeed</title>
        {_shell_head(_appearance_choice(user_key))}
      </head>
      <body{_dearrow_body_attrs(user_key)}>
        <a class="skip-link" href="#main">Skip to content</a>
        {_app_header("", user, user_key)}
        {_app_sidebar("home", user_key)}
        <div class="nav-scrim" data-nav-scrim aria-hidden="true"></div>
        <main class="home-main" id="main" tabindex="-1">
          <h1 class="sr-only">Home</h1>
          <div class="topic-sentinel" data-topic-sentinel aria-hidden="true"></div>
          <div class="topic-wrap" role="group" aria-label="Recommendation filters" data-topic-wrap>
            <div class="topic-row" data-topic-row>{_topic_chips(all_for_channels, feed_filter, user_key)}</div>
            <button class="topic-next" type="button" data-topic-next aria-label="More filters" tabindex="-1">{_icon("chevron-right")}</button>
            <span class="topic-divider" aria-hidden="true"></span>
            <button class="topic-chip topic-action" type="button" data-open-feed-dialog aria-label="Personalize feed" title="Personalize feed">{_icon("tune")}</button>
          </div>
          <section class="creator-strip" aria-label="Channels"{' hidden' if feed_filter != "all" else ''}>{creator_strip}</section>
          {_setup_checklist(user_key)}
          <div id="home-feed-sections" data-feed-container data-next-page="1" data-feed-filter="{escape(feed_filter, quote=True)}">
            {initial_sections}
          </div>
          {_feed_loading("Loading more recommendations", "data-feed-loading", f'<div class="video-grid">{_skeleton_cards(4)}</div>')}
          <div class="feed-retry" data-feed-retry hidden><span>Could not load more videos.</span><button type="button" data-retry-feed>Retry</button></div>
          <div class="feed-sentinel" data-feed-sentinel aria-hidden="true"></div>
        </main>
        <nav class="mobile-bottom-nav" aria-label="Mobile navigation">{_mobile_nav()}</nav>
        {_feed_dialog(user_key)}
        {_settings_dialog(user, user_key)}
        {_playlist_dialogs()}
        {_collection_dialogs()}
        {_shell_foot()}
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
    video = db.get_video_by_youtube_id(youtube_video_id, user_key)
    if not video:
        return _page_not_found(request, "Video not found", "OpenFeed has no video with this ID. The link may be wrong, or it was never imported here.", "/", "Back to Home", "")
    if _kids_profile(user_key) and _age_restricted(video):
        return _kids_refusal(request, "This video is not available on a kids profile",
                             "YouTube marks it as age-restricted. Switch to another profile to watch it.")
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
            video = db.get_video_by_youtube_id(youtube_video_id, user_key) or video
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
    view_text = _count_label(escape(views), "view") if views else "Views unavailable"
    subscribers = _compact_count(video.get("channel_subscriber_count"))
    subscriber_text = _count_label(escape(subscribers), "subscriber") if subscribers else ""
    # Views and the release date live here rather than under the channel name — they
    # describe the video, not the person who made it.
    description_meta = " &middot; ".join(part for part in [view_text, published_text] if part)
    subscription = escape(video.get("subscription_title") or _source_label(video))
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
    resume_at, resuming = _watch_start_point(request.query_params.get("t"), restart, stored_resume)
    resume_query = f"?t={resume_at}" if resume_at else ""
    resume_label = _duration_text(resume_at) if resume_at else ""
    # Said in the page, under the player, rather than in a toast: a toast sat on the
    # description (desktop) or the first Up next card (phone) for six seconds.
    resume_note = (
        f'<div class="resume-note" data-resume-note role="status">{_icon("history")}'
        f'<span>Resuming from <strong>{escape(resume_label)}</strong></span>'
        f'<a class="resume-note-action" href="/watch/{escape(youtube_video_id, quote=True)}?restart=1">Start over</a>'
        f'<button class="resume-note-close" type="button" data-resume-dismiss aria-label="Dismiss">{_icon("x")}</button></div>'
        if resuming else ""
    )

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
    # The name says whether the button is on, the same as aria-pressed, and the figure is
    # named as YouTube's: a like here stays on this server, so it is never added to it.
    like_count_note = f", {like_total} likes on YouTube" if like_total else ""
    like_label = escape(("Liked" if reaction == "like" else "Like") + like_count_note, quote=True)
    dislike_label = "Disliked" if reaction == "dislike" else "Dislike"
    # An RYD figure is an estimate reconstructed by a third party, not a number YouTube
    # published, and the tooltip says so rather than presenting it as fact.
    dislike_from_ryd = str(video.get("dislike_source") or "") == "ryd"
    dislike_title = (
        "Estimated by Return YouTube Dislike, not an official count"
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
    # Paused watch history (History -> Pause watch history) records nothing about watching:
    # no history entry, no resume point and no recommendation signal.
    if not library_tools.history_paused(user_key):
        db.record_video_event(youtube_video_id, "watch_page_open", {"source": "watch_page", "weight": 0}, user_key)
        # "Help tune recommendations": credit the interleaving team whose shelf showed this video.
        # Only marks an impression that exists, so it is a no-op when the comparison is off.
        _record_interleave_click(user_key, youtube_video_id)
        db.update_playback_progress(youtube_video_id, 0, video.get("duration_seconds"), "open", {"source": "watch_page"}, user_key)

    # Ranked against the video on screen and with shorts left out - they have their own
    # vertical viewer, and a 30-second clip is not a sensible follow-on from a long video.
    related = db.list_related_videos(youtube_video_id, 14, user_key)
    # Play all / Shuffle from a playlist or Liked: ?list= keeps the list queued above Up next.
    list_shuffle = re.sub(r"[^0-9a-f]", "", str(request.query_params.get("shuffle") or ""))[:16]
    list_panel = _watch_list_panel(
        _resolve_watch_list(str(request.query_params.get("list") or ""), user_key), youtube_video_id, list_shuffle,
    ) if request.query_params.get("list") else ""
    suggestion_items = [_media_row(candidate, settings, _source_label(candidate)) for candidate in related]
    suggestions_html = "".join(suggestion_items) or '<p class="chat-placeholder">No suggestions available yet.</p>'
    # A filter is only worth showing when it would change the list.
    same_channel = sum(1 for candidate in related if str(candidate.get("youtube_channel_id") or "") == channel_id_raw) if channel_id_raw else 0
    upnext_filters = (
        '<div class="segmented upnext-filters" role="group" aria-label="Filter up next">'
        '<button class="segment" type="button" data-upnext-filter="all" aria-pressed="true">All</button>'
        '<button class="segment" type="button" data-upnext-filter="channel" aria-pressed="false">This channel</button></div>'
        if 0 < same_channel < len(related) else ""
    )
    chapter_card = _chapter_card(_parse_chapters(summary_text, video.get("duration_seconds")))
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
    # Comments are read-only: Invidious can read them but nothing can be posted, so there
    # is deliberately no composer. At phone width the preview row opens this as a sheet.
    comments_html = (
        f'''<section class="comments" data-comments-for="{escape(youtube_video_id)}">
          <button class="comments-preview" type="button" data-comments-open aria-controls="comments-panel" aria-expanded="false">
            <span class="comments-preview-head"><strong>Comments</strong><span class="comments-count" data-comments-count></span>{_icon("chevron-right")}</span>
            <span class="comments-preview-line" data-comments-preview></span>
          </button>
          <div class="comments-panel" id="comments-panel" data-comments-panel>
            <div class="comments-sheet-handle" data-comments-handle aria-hidden="true"></div>
            <div class="comments-head">
              <h2>Comments</h2><span class="comments-count" data-comments-count></span>
              <div class="segmented comment-sorts" role="group" aria-label="Sort comments">
                <button class="segment" type="button" data-comment-sort="top" aria-pressed="true">Top</button>
                <button class="segment" type="button" data-comment-sort="new" aria-pressed="false">Newest</button>
                <button class="segment" type="button" data-comment-sort="timed" aria-pressed="false">Timed</button>
              </div>
              <button class="btn btn-ghost btn-icon comments-close" type="button" data-comments-close aria-label="Close comments">{_icon("x")}</button>
            </div>
            <p class="comments-note" data-comments-note hidden></p>
            <div class="comments-body" data-comments-body>
              {_comment_skeleton_rows()}
            </div>
          </div>
        </section>'''
    ) if show_comments else ""
    chat_panel = _chat_panel(youtube_video_id, user_key) if not short_class else ""

    content = f'''
      <div class="watch-layout" data-watch-layout data-watch-channel="{channel_id}" data-video-id="{escape(youtube_video_id)}"{details_pending_attr} data-duration="{int(video.get("duration_seconds") or 0)}" data-resume="{resume_at}">
        <div class="watch-stage">
          <div class="watch-player-shell{short_class}">
            <iframe class="player-frame" src="/watch/{escape(youtube_video_id)}/player{resume_query}" title="Video player" allow="autoplay; fullscreen; picture-in-picture" allowfullscreen></iframe>
          </div>
        </div>
        <article class="watch-content">
          {resume_note}
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
              <button class="pill-action reaction-action has-count{like_selected}" type="button" data-feedback="like" data-video-id="{escape(youtube_video_id)}" aria-label="{like_label}" data-label-note="{escape(like_count_note, quote=True)}" data-tip="{"Liked" if reaction == "like" else "Like"}" aria-pressed="{like_pressed}">{_icon("like")}{like_count_html}<span class="sr-only">Like</span></button>
              <button class="pill-action reaction-action has-count{dislike_selected}" type="button" data-feedback="dislike" data-video-id="{escape(youtube_video_id)}" aria-label="{dislike_label}" data-label-note="" data-tip="{dislike_label}" aria-pressed="{dislike_pressed}">{_icon("dislike")}{dislike_count_html}<span class="sr-only">Dislike</span></button>
            </div>
            <button class="btn btn-icon" type="button" data-share-open aria-haspopup="dialog" aria-label="Share" data-tip="Share">{_icon("share")}</button>
            <button class="btn btn-icon" type="button" data-save-to-playlist="{escape(youtube_video_id)}" data-video-title="{escape(_text_excerpt(title_text, 90), quote=True)}" aria-label="Save to playlist" data-tip="Save">{_icon("playlist")}</button>
            {_watch_options_menu(youtube_video_id, invidious_url, youtube_url, title_text, watched=_is_watched(progress_row), can_download=_may_download(request))}
            </div>
          </div>
          {description_html}
          {f'<dialog class="feed-dialog details-dialog" data-details-dialog aria-labelledby="details-dialog-title"><div class="fc-head"><div><p class="fc-kicker">Video</p><h2 id="details-dialog-title">Details</h2></div><button class="icon-button" type="button" data-close-dialog aria-label="Close">{_icon("x")}</button></div><div class="fc-body">{details_html}</div></dialog>' if details_html else ""}
          {comments_html}
        </article>
        <aside class="watch-panel" aria-label="Watch side panel">
          {chat_panel}
          {list_panel}
          {chapter_card}
          <div class="upnext-head">{f'<div class="tabbar" role="tablist" aria-label="Watch panels"><button class="tab active" type="button" data-tab="suggestions">Up next</button>{transcript_button}</div>' if transcript_button else '<h2>Up next</h2>'}{upnext_filters}</div>
          <section id="tab-suggestions" class="tab-panel active">{suggestions_html}</section>
          {transcript_panel}
        </aside>
      </div>
      {_download_dialog()}
      {_watch_share_dialog(youtube_video_id, user_key, bool(short_class))}
    '''
    return _app_shell(title_text, "", content, main_class="watch-main", request=request, immersive=True)


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


def _on_request_sources(user_key: str = "local") -> list[dict[str, Any]]:
    """Third-party players this user opted into: public Invidious instances, then YouTube.

    Each one sends the viewer's browser to someone else, so each needs the admin to allow
    it AND the user to switch it on in Settings → Privacy, and none of them is ever part of
    the automatic ladder: the player offers them only as a button the viewer presses after
    the private sources failed. Never raises; a read failure means none.
    """
    settings = get_settings()
    try:
        choices = privacy.get_choices(user_key)
        preferences = db.get_playback_preferences(user_key)
    except Exception as exc:  # noqa: BLE001 - no answer means no third-party player
        logger.info("on-request sources unavailable: %s", exc)
        return []
    sources: list[dict[str, Any]] = []
    if privacy.public_instances_enabled_for(choices):
        own = list(preferences.get("public_invidious_urls") or []) if privacy.user_instances_allowed() else []
        for base in own + settings.invidious_fallback_list:
            origin = privacy.instance_origin(base)
            host = urllib.parse.urlparse(base).netloc or base
            source_id = f"invidious:{host}"
            if not origin or any(source["id"] == source_id for source in sources):
                continue
            sources.append({"id": source_id, "label": host, "kind": "invidious-public", "base": base.rstrip("/"),
                            "origin": origin, "on_request": True, "enabled": True,
                            "hint": privacy.PUBLIC_INSTANCE_WARNING, "warning": privacy.PUBLIC_INSTANCE_WARNING})
    if privacy.youtube_enabled_for(choices):
        sources.append({"id": "youtube", "label": "YouTube", "kind": "youtube", "base": "",
                        "origin": privacy.YOUTUBE_EMBED_ORIGIN, "on_request": True, "enabled": True,
                        "hint": privacy.YOUTUBE_WARNING, "warning": privacy.YOUTUBE_WARNING})
    return sources


def _all_playback_sources(user_key: str = "local", downloaded: bool = False) -> list[dict[str, Any]]:
    """Every private source this user *could* play from, enabled or not, in their chosen order.

    Disabled sources are still returned so Settings can list them with the switch off —
    a source you cannot see is a source you cannot turn back on. Third-party players are
    not here: they are never tried automatically. See _on_request_sources.
    """
    settings = get_settings()
    preferences = db.get_playback_preferences(user_key)
    disabled = set(preferences.get("disabled_sources") or [])
    sources: list[dict[str, Any]] = []

    if settings.invidious_base_url:
        sources.append({"id": "invidious", "label": "Invidious (local)", "kind": "invidious-local", "base": "",
                        "hint": "Your own instance. Private, but depends on YouTube accepting its token."})

    # A file already on disk beats every network source: it always works, it is the
    # quality the user chose, and it needs nothing from YouTube. Only offered for videos
    # that actually have one, so this entry appears and disappears per video.
    if downloaded:
        sources.append({"id": "downloaded", "label": "Downloaded copy", "kind": "downloaded", "base": "",
                        "hint": "Plays the file saved in your library. Works offline and never touches YouTube."})

    if settings.ytdlp_enabled and settings.ytdlp_playback_fallback and ytdlp.available():
        sources.append({"id": "ytdlp", "label": "Direct stream (yt-dlp)", "kind": "ytdlp", "base": "",
                        "hint": "Extracts the stream itself and proxies it through OpenFeed. Private, "
                                "but limited to the muxed rendition, usually 360p or 720p."})

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


def _on_request_player_sources(youtube_video_id: str, user_key: str = "local", start: int = 0) -> list[dict[str, str]]:
    """The third-party players to offer as buttons in the player's failure card.

    Pressing one is the play intent, so they always autoplay.
    """
    video_id = urllib.parse.quote(youtube_video_id, safe="")
    return [
        {
            "id": source["id"], "label": source["label"], "kind": source["kind"],
            "url": _source_url(source, video_id, "1", start), "same_origin": "",
            "origin": source["origin"], "warning": source["warning"],
        }
        for source in _on_request_sources(user_key)
    ]


def _player_media_meta(youtube_video_id: str) -> dict[str, str]:
    """Title and channel for the lock screen and notification controls (Media Session)."""
    try:
        video = db.get_video_by_youtube_id(youtube_video_id) or {}
    except Exception as exc:  # noqa: BLE001 - decoration, never a reason to fail the player
        logger.info("no media metadata for %s: %s", youtube_video_id, exc)
        return {}
    return {
        "title": str(video.get("title") or ""),
        "channel": str(video.get("channel_title") or video.get("author") or ""),
    }


def _player_chapters(youtube_video_id: str) -> list[dict[str, Any]]:
    """The chapters to cut into the scrubber. A player must still open without them."""
    try:
        video = db.get_video_by_youtube_id(youtube_video_id) or {}
    except Exception as exc:  # noqa: BLE001 - decoration on the scrubber, never a reason to fail
        logger.info("no chapters for %s: %s", youtube_video_id, exc)
        return []
    text = _description_text(video.get("summary") or video.get("description") or "")
    return _parse_chapters(text, video.get("duration_seconds"))


@app.get("/watch/{youtube_video_id}/player", response_class=HTMLResponse)
def watch_player(request: Request, youtube_video_id: str, t: int = 0, autoplay: int = 1, short: int = 0):
    """The player shell.

    `autoplay=0` is what makes Shorts preloading possible: the frame fetches the player
    and buffers, but waits for an openfeed:play message before it starts. `short=1` drops
    the control bar, because the Shorts page draws its own progress line and rail.
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
    # Third-party players the viewer opted into. Only these origins may be framed by this
    # response, and only after the viewer presses the button that names them.
    on_request = _on_request_player_sources(youtube_video_id, user_key, start)
    request.state.frame_origins = [source["origin"] for source in on_request]
    # With nothing to try there is no ladder to walk, so the walk of the chain happens here
    # and the failure card opens straight away instead of waiting on a client fetch.
    diagnosis = _diagnose_playback(youtube_video_id) if not sources else None
    chapters = [] if short else _player_chapters(youtube_video_id)
    diagnosis_attr = f' data-diagnosis="{escape(json.dumps(diagnosis), quote=True)}"' if diagnosis else ""
    chapters_attr = f' data-chapters="{escape(json.dumps(chapters), quote=True)}"' if chapters else ""
    short_attr = ' data-short="1"' if short else ""
    media_meta = {} if short else _player_media_meta(youtube_video_id)
    if not short:
        media_meta["default-speed"] = _default_speed_attr(user_key)
    media_attr = "".join(
        f' data-{name}="{escape(value, quote=True)}"' for name, value in media_meta.items() if value
    )
    popout_label = "Open in Invidious" if invidious_url else "Open on YouTube"
    on_request_attr = f' data-on-request="{escape(json.dumps(on_request), quote=True)}"' if on_request else ""

    shell_styles = """
          html, body { margin: 0; width: 100%; height: 100%; background: #000; color: #ededef;
            font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Inter", "Segoe UI", Roboto, system-ui, sans-serif;
            letter-spacing: -.006em; overflow: hidden; }
          iframe { width: 100%; height: 100%; border: 0; background: #000; display: block; }
          button { font: inherit; }
          /* Top-right so it never covers the player's own controls (fullscreen sits bottom-right). */
          .popout { position: absolute; top: 10px; right: 10px; z-index: 3; width: 36px; height: 36px; display: grid; place-items: center;
            background: rgba(10,10,11,.72); border: 1px solid rgba(255,255,255,.09); border-radius: 999px;
            opacity: 0; transition: opacity .16s ease, background .16s ease; }
          body:hover .popout, .popout:focus-within { opacity: 1; }
          .popout:hover { background: #232327; }
          .popout a { color: #ededef; width: 100%; height: 100%; display: grid; place-items: center; text-decoration: none; border-radius: inherit; }
          .popout a:focus-visible, .action:focus-visible, .source-note button:focus-visible { outline: 0; box-shadow: 0 0 0 2px rgba(255,255,255,.5); }
          .popout svg { width: 16px; height: 16px; fill: none; stroke: currentColor; stroke-width: 2;
            stroke-linecap: round; stroke-linejoin: round; }
          body.is-short .popout, body.is-short .source-note { display: none; }
          @media (hover: none) { .popout { display: none; } }

          .stage { position: absolute; inset: 0; background: #000; }
          /* Hidden until our chrome is on it, so stock video.js never flashes. */
          .stage iframe { position: absolute; inset: 0; z-index: 2; opacity: 0; }
          .stage.is-ready iframe { opacity: 1; }
          .poster { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover;
            opacity: .35; transition: opacity .2s ease; }
          .stage.is-ready .poster { opacity: 0; }
          .load-line { position: absolute; left: 0; right: 0; bottom: 0; height: 2px; z-index: 3; overflow: hidden; background: #1a1a1d; }
          .load-line::after { content: ""; position: absolute; top: 0; bottom: 0; left: -40%; width: 40%; background: #ededef; animation: load-line 1.1s linear infinite; }
          .stage.is-ready .load-line, .stage.is-exhausted .load-line, .load-line[hidden] { display: none; }
          @keyframes load-line { to { left: 100%; } }
          @media (prefers-reduced-motion: reduce) { .load-line::after { animation: none; left: 0; width: 100%; background: #232327; } }
          /* What the ladder is doing right now, in words, while there is no picture. */
          .ladder-status { position: absolute; left: 12px; right: 12px; bottom: 12px; z-index: 3; margin: 0; color: #ededef; font-size: 12.5px; line-height: 1.4; }
          .ladder-status[hidden], .stage.is-ready .ladder-status { display: none; }
          .source-note { position: absolute; left: 10px; top: 10px; z-index: 3; display: flex; align-items: center; gap: 8px; max-width: calc(100% - 76px);
            min-height: 32px; box-sizing: border-box; padding: 4px 6px 4px 11px; border-radius: 999px; background: rgba(10,10,11,.72); border: 1px solid rgba(255,255,255,.09);
            font-size: 11.5px; color: #ededef; opacity: 0; transition: opacity .18s ease; }
          .source-note > span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
          body:hover .source-note, .source-note:focus-within { opacity: 1; }
          .source-note[hidden] { display: none; }
          .source-note button { flex: 0 0 auto; border: 0; border-radius: 999px; background: #1a1a1d; color: #ededef;
            font-size: 11.5px; font-weight: 550; padding: 3px 10px; cursor: pointer; }
          .source-note button:hover { background: #232327; }
          .source-note button[hidden] { display: none; }

          /* Failure is the frame's content, not a dialog over it. */
          .fail { position: absolute; inset: 0; z-index: 4; display: flex; padding: 12px; box-sizing: border-box; background: #000; overflow: auto; }
          .fail[hidden] { display: none; }
          .fail-panel { box-sizing: border-box; width: 100%; max-width: 680px; margin: auto; padding: 14px 16px; display: grid; gap: 10px;
            border: 1px solid rgba(255,255,255,.09); border-radius: 14px; background: rgba(229,72,77,.12); }
          .fail-cause { margin: 0; color: #f2686c; font-size: 15px; font-weight: 600; line-height: 1.35; letter-spacing: -.012em; }
          .fail-detail { margin: 0; color: #ededef; font-size: 12.5px; line-height: 1.5; }
          .fail-detail:empty, .remedies:empty, .ladder:empty { display: none; }
          .hops { margin: 0; padding: 0; list-style: none; display: flex; flex-wrap: wrap; align-items: center; gap: 4px 0; }
          .hop { display: inline-flex; align-items: center; gap: 6px; color: #9a9aa3; font-size: 11.5px; font-weight: 550; white-space: nowrap; }
          .hop + .hop::before { content: ""; width: 18px; height: 1px; margin: 0 6px 0 2px; background: rgba(255,255,255,.18); }
          .dot { flex: 0 0 auto; width: 8px; height: 8px; box-sizing: border-box; border-radius: 999px; border: 1px solid #84848d; }
          .hop.ok { color: #ededef; }
          .hop.bad { color: #f2686c; }
          .is-ok > .dot, .ok > .dot { background: #3fb950; border-color: #3fb950; }
          .is-bad > .dot, .bad > .dot { background: #e5484d; border-color: #e5484d; }
          .is-hold > .dot { background: #d29922; border-color: #d29922; }
          .wait > .dot { background: #232327; border-color: #232327; animation: wait 1s ease-in-out infinite alternate; }
          @keyframes wait { to { opacity: .4; } }
          @media (prefers-reduced-motion: reduce) { .wait > .dot { animation: none; } }
          .ladder { margin: 0; padding: 0; list-style: none; display: grid; gap: 2px; }
          .ladder li { display: grid; grid-template-columns: 8px minmax(0, max-content) minmax(0, 1fr) auto; align-items: baseline; gap: 8px; font-size: 12px; line-height: 1.45; }
          .ladder .dot { align-self: center; }
          .ladder strong { font-weight: 550; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
          .ladder span { color: #9a9aa3; }
          .ladder .why { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
          .ladder .ms { font-variant-numeric: tabular-nums; }
          .remedies { margin: 0; padding: 0 0 0 16px; color: #ededef; font-size: 12px; line-height: 1.5; }
          .fail-actions { display: flex; flex-wrap: wrap; gap: 6px; }
          /* Third-party players: the button and, beside it, who sees what. */
          .on-request { display: grid; gap: 6px; }
          .on-request[hidden] { display: none; }
          .on-request-row { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 10px; }
          .on-request-row small { color: #c9c9cf; font-size: 12px; line-height: 1.4; }
          .action { min-height: 32px; display: inline-flex; align-items: center; padding: 0 14px; border: 1px solid transparent;
            border-radius: 999px; background: #1a1a1d; color: #ededef; font-size: 12.5px; font-weight: 550; white-space: nowrap;
            cursor: pointer; text-decoration: none; transition: background .14s ease, transform .14s cubic-bezier(.2,.8,.3,1); }
          .action:hover { background: #232327; border-color: rgba(255,255,255,.055); }
          .action:active { transform: scale(.96); }
          .action[hidden] { display: none; }
          .action.ghost { background: transparent; color: #ededef; }
          .action.ghost:hover { background: #1a1a1d; }
          @media (prefers-reduced-motion: reduce) { .action { transition: none; } .action:active { transform: none; } }
          @media (pointer: coarse) { .action { min-height: 44px; } .source-note button { min-height: 32px; } }
          /* A phone frame is about 390x219: cause, hops and actions must survive, in that order. */
          @media (max-height: 300px) {
            .fail { padding: 6px; }
            .fail-panel { padding: 8px 10px; gap: 6px; border-radius: 10px; }
            .fail-cause { font-size: 13px; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
            .fail-detail, .remedies, .ladder { display: none; }
            .fail-actions { flex-wrap: nowrap; overflow-x: auto; scrollbar-width: none; margin: 0 -10px; padding: 0 10px; }
            .fail-actions::-webkit-scrollbar { display: none; }
            .action { flex: 0 0 auto; }
            .hop + .hop::before { width: 10px; margin: 0 4px 0 0; }
            .hop { font-size: 11px; }
            .hop.skip > span:last-child { display: none; }
          }
          /* A start that never came: over the frame, not instead of it, since it may yet play. */
          .stall { position: absolute; left: 50%; top: 50%; z-index: 4; transform: translate(-50%, -50%); box-sizing: border-box;
            width: min(440px, calc(100% - 24px)); padding: 12px 14px; display: grid; gap: 10px; border-radius: 14px;
            background: rgba(10,10,11,.88); border: 1px solid rgba(255,255,255,.09); }
          .stall[hidden] { display: none; }
          .stall p { margin: 0; color: #ededef; font-size: 13px; line-height: 1.45; }
          @media (max-height: 300px) { .stall { padding: 8px 10px; gap: 6px; } .stall p { font-size: 12px; } }
          body.is-short .fail-panel { padding: 12px; }
          body.is-short .hop + .hop::before { display: none; }
          body.is-short .hops { gap: 4px 10px; }
    """

    poster = "" if not sources else f'<img class="poster" src="/vi/{video_id}/hqdefault.jpg" alt="" aria-hidden="true">'
    body_html = f"""
        <div class="stage" data-player-stage data-sources="{escape(json.dumps(sources), quote=True)}" data-video-id="{video_id}" data-fallback-reason="{escape(reason, quote=True)}"{diagnosis_attr}{chapters_attr}{short_attr}{media_attr}{on_request_attr}>
          {poster}
          <div class="load-line" data-player-spinner aria-hidden="true"></div>
          <p class="ladder-status" data-ladder-status role="status" hidden></p>
        </div>
        <section class="fail" data-fail-card aria-label="Playback failed" hidden>
          <div class="fail-panel" role="alert">
            <h1 class="fail-cause" data-fail-cause></h1>
            <p class="fail-detail" data-fail-detail></p>
            <ol class="hops" data-fail-hops aria-label="Playback chain"></ol>
            <ul class="ladder" data-fail-sources aria-label="Sources tried"></ul>
            <ul class="remedies" data-fail-remedies></ul>
            <div class="on-request" data-on-request-list hidden></div>
            <div class="fail-actions">
              <button class="action" type="button" data-fail-retry>Retry</button>
              <button class="action" type="button" data-fail-next hidden>Next source</button>
              <button class="action ghost" type="button" data-fail-diagnostics>Open diagnostics</button>
              <button class="action ghost" type="button" data-fail-copy>Copy report</button>
              <a class="action ghost" href="{youtube_url}" target="_blank" rel="noreferrer noopener">Open on YouTube</a>
            </div>
          </div>
        </section>
        <div class="source-note" data-source-note hidden><span data-source-label></span><button type="button" data-retry-preferred>Retry Invidious</button></div>
        <div class="stall" data-stall role="alert" hidden>
          <p data-stall-text></p>
          <div class="fail-actions">
            <button class="action" type="button" data-stall-next>Try the next source</button>
            <button class="action ghost" type="button" data-stall-wait>Keep waiting</button>
          </div>
        </div>
        <div class="popout"><a href="{invidious_url or youtube_url}" target="_blank" rel="noreferrer noopener" aria-label="{popout_label}" title="{popout_label}">
          <svg viewBox="0 0 24 24" aria-hidden="true">{ICON_PATHS["external"]}</svg>
        </a></div>
        """

    # No preconnect or dns-prefetch hints: a hint to a third-party host opens a connection
    # from the viewer's browser before anyone chose to play from there.
    return f"""
    <!doctype html>
    <html lang="en">
      <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>{shell_styles}</style>
      </head>
      <body>
        {body_html}
        <script>{_bundle(_ASSET_DIR / "player" / "kit", ".js")}</script>
        <script>{_player_script()}</script>
      </body>
    </html>
    """


def _player_chrome_css() -> str:
    """The chrome for every player we can reach. See assets/player/chrome.css."""
    return _bundle(_ASSET_DIR / "player", ".css")


def _native_player_page(src: str, tag: str, autoplay: bool) -> HTMLResponse:
    """A plain <video> wearing the same chrome as the video.js player.

    `src` and `tag` are already escaped. Native controls stay on until the script has
    replaced them, so a script failure still leaves a usable player.
    """
    rates = "".join(
        f'<li class="vjs-menu-item" role="menuitemradio" tabindex="0" data-rate="{rate}" aria-checked="false">{rate}x</li>'
        for rate in ("2", "1.75", "1.5", "1.25", "1", "0.75", "0.5")
    )
    play = " autoplay" if autoplay else ""
    return HTMLResponse(f'''<!doctype html><html lang="en"><head><meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1"><title>OpenFeed player</title>
      <style>
        html, body {{ margin: 0; width: 100%; height: 100%; background: #000; overflow: hidden; }}
        .of-native {{ position: relative; width: 100%; height: 100%; background: #000; color: #ededef; font-size: 10px; }}
        .of-native video {{ display: block; width: 100%; height: 100%; background: #000; }}
        .of-native:not(.vjs-enhanced) > :not(video) {{ display: none !important; }}
        .of-native .vjs-control-bar {{ display: flex; transition: opacity .18s ease; }}
        .of-native.vjs-user-inactive.vjs-playing .vjs-control-bar {{ opacity: 0; pointer-events: none; }}
        .of-native.vjs-user-inactive.vjs-playing {{ cursor: none; }}
        .of-native .vjs-menu {{ display: none; position: absolute; }}
        .of-native .vjs-playback-rate:hover .vjs-menu, .of-native .vjs-menu.vjs-lock-showing {{ display: block; }}
        .of-native .vjs-menu-content {{ margin: 0; list-style: none; }}
        .of-native .vjs-menu li {{ cursor: pointer; }}
        .of-native .vjs-big-play-button {{ z-index: 2; }}
        .of-native.vjs-has-started .vjs-big-play-button {{ display: none; }}
        .of-native [hidden] {{ display: none !important; }}
      </style>
      <style id="openfeed-embed-style">{_player_chrome_css()}</style></head><body>
      <div class="video-js of-native vjs-paused" data-native-player>
        <video controls playsinline preload="metadata"{play} src="{src}"></video>
        <button class="vjs-big-play-button" type="button" aria-label="Play"><span class="vjs-icon-placeholder" aria-hidden="true"></span></button>
        <div class="vjs-loading-spinner" aria-hidden="true"></div>
        <div class="vjs-control-bar" dir="ltr">
          <button class="vjs-play-control vjs-control vjs-button" type="button" data-tip="Play" aria-label="Play"><span class="vjs-icon-placeholder" aria-hidden="true"></span></button>
          <div class="vjs-volume-panel vjs-control">
            <button class="vjs-mute-control vjs-control vjs-button vjs-vol-3" type="button" data-tip="Mute" aria-label="Mute"><span class="vjs-icon-placeholder" aria-hidden="true"></span></button>
            <div class="vjs-volume-control vjs-control"><div class="vjs-volume-bar vjs-slider" role="slider" tabindex="0" aria-label="Volume" aria-valuemin="0" aria-valuemax="100" aria-valuenow="100"><div class="vjs-volume-level"></div></div></div>
          </div>
          <div class="vjs-current-time vjs-time-control"><span class="vjs-current-time-display">0:00</span></div>
          <div class="vjs-time-control vjs-time-divider" aria-hidden="true">/</div>
          <div class="vjs-duration vjs-time-control"><span class="vjs-duration-display">0:00</span></div>
          <div class="vjs-progress-control vjs-control">
            <div class="vjs-progress-holder vjs-slider" role="slider" tabindex="0" aria-label="Seek" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0">
              <div class="vjs-load-progress"></div>
              <div class="vjs-mouse-display"><div class="vjs-time-tooltip" aria-hidden="true"></div></div>
              <div class="vjs-play-progress"></div>
            </div>
          </div>
          <div class="vjs-spacer"></div>
          <span class="of-source-tag">{tag}</span>
          <div class="vjs-playback-rate vjs-menu-button vjs-menu-button-popup vjs-control">
            <div class="vjs-playback-rate-value">1x</div>
            <button class="vjs-button" type="button" data-tip="Speed" aria-label="Playback speed" aria-haspopup="true" aria-expanded="false"><span class="vjs-icon-placeholder" aria-hidden="true"></span></button>
            <div class="vjs-menu"><ul class="vjs-menu-content" role="menu">{rates}</ul></div>
          </div>
          <button class="of-pip-control vjs-control vjs-button" type="button" data-tip="Picture in picture" aria-label="Picture in picture"><span class="vjs-icon-placeholder" aria-hidden="true"></span></button>
          <button class="vjs-fullscreen-control vjs-control vjs-button" type="button" data-tip="Fullscreen" aria-label="Fullscreen"><span class="vjs-icon-placeholder" aria-hidden="true"></span></button>
        </div>
      </div>
      <script>{_bundle(_ASSET_DIR / "player", ".js")}</script>
    </body></html>''')


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
        const parseData = (raw, fallback) => {
          try { return raw ? JSON.parse(raw) : fallback; } catch (error) { return fallback; }
        };
        const sources = parseData(stage.dataset.sources, []);
        const chapters = parseData(stage.dataset.chapters, []);
        const VIDEO_ID = stage.dataset.videoId || '';
        const IS_SHORT = stage.dataset.short === '1';
        if (IS_SHORT) document.body.classList.add('is-short');

        const note = document.querySelector('[data-source-note]');
        const noteLabel = document.querySelector('[data-source-label]');
        const spinner = document.querySelector('[data-player-spinner]');
        const ladderStatus = document.querySelector('[data-ladder-status]');
        const START_DEADLINE = 9000;
        const LOAD_DEADLINE = 12000;
        /* Invidious's dash player retries its own source after an error, so a failure only
           counts once it has had this long to recover. */
        const RECOVERY_GRACE = 12000;
        let index = 0;
        let frame = null;
        let timer = null;
        let settled = false;
        let autoWalk = true;
        let resumeAt = 0;

        const clock = (seconds) => {
          const total = Math.max(0, Math.floor(Number(seconds) || 0));
          const h = Math.floor(total / 3600);
          const m = Math.floor((total % 3600) / 60);
          const s = String(total % 60).padStart(2, '0');
          return h ? `${h}:${String(m).padStart(2, '0')}:${s}` : `${m}:${s}`;
        };
        const postUp = (message) => {
          if (window.parent !== window) window.parent.postMessage(message, window.location.origin);
        };
        function setStatus(text) {
          if (!ladderStatus) return;
          ladderStatus.textContent = text;
          ladderStatus.hidden = !text;
        }

        /* One line per source, kept for the failure card and the copied report. */
        const REASONS = {
          'no video element': 'Player page had no video',
          'media error': 'Browser reported a media error',
          'never started': `Did not start within ${START_DEADLINE / 1000}s`,
          'load timeout': `Player did not load within ${LOAD_DEADLINE / 1000}s`,
          'frame error': 'Player frame failed to load',
          'csp': "Blocked by this page's Content-Security-Policy (frame-src)",
        };
        const attempts = new Map();
        function beginAttempt(source) {
          attempts.set(source.id, { state: 'trying', why: '', ms: 0, started: performance.now() });
        }
        function endAttempt(source, state, why) {
          const attempt = attempts.get(source.id) || { started: performance.now() };
          attempt.state = state;
          attempt.why = why;
          attempt.ms = Math.round(performance.now() - attempt.started);
          attempts.set(source.id, attempt);
        }

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

        function markReady(source, unconfirmed = false) {
          settled = true;
          clearTimer();
          /* Showing a player that is waiting for a tap proves nothing about the source. */
          if (!unconfirmed) report(source, true);
          endAttempt(source, 'ok', '');
          setStatus('');
          postUp({ type: 'openfeed:playback-ready' });
          stage.classList.add('is-ready');
          if (spinner) spinner.hidden = true;
          if (note && noteLabel) {
            /* Say so whenever playback is not coming from the local Invidious, and say why. */
            const preferred = source.id === 'invidious';
            note.hidden = preferred;
            const why = stage.dataset.fallbackReason || '';
            const base = preferred ? '' : `Playing via ${source.label}${why ? `: ${why}` : ''}`;
            noteLabel.textContent = base;
            if (!source.same_origin) {
              postUp({ type: 'openfeed:opaque-player' });
              /* Nothing inside a third-party player is reachable from here: we cannot read
                 its clock, so we cannot skip anything or draw a marker on its scrubber.
                 Saying that is better than a SponsorBlock toggle that silently does
                 nothing on exactly the videos that needed the fallback. */
              sbFetch().then((segments) => {
                if (!segments.length || noteLabel.textContent !== base) return;
                noteLabel.textContent = `${base}. SponsorBlock cannot skip in this player`;
              });
            }
          }
        }

        /* --- Failure -------------------------------------------------------------------
           Every source failed, or a playing one died. Reporting it healthy (the old path)
           lied to the health tracker and faded the spinner over a black stage, so the
           frame shows the failure instead and the last ok:false report stands. The card
           is drawn from real data only: the attempts above, source health, and the
           server's walk of the Invidious chain. */
        const card = document.querySelector('[data-fail-card]');
        const part = (name) => card.querySelector(`[data-fail-${name}]`);

        /* Third-party players the viewer opted into in Settings > Privacy. Never part of the
           automatic walk: each is a button that says where it plays from and who sees it,
           and only a press puts it into the ladder. */
        const onRequest = parseData(stage.dataset.onRequest, []);
        const isOnRequest = (source) => Boolean(source && source.origin);
        /* The next private source after `from`, wrapping, or -1 when there is none. */
        function nextPrivate(from) {
          for (let step = 1; step < sources.length; step += 1) {
            const at = (from + step) % sources.length;
            if (!isOnRequest(sources[at])) return at;
          }
          return -1;
        }
        function drawOnRequest() {
          const holder = card.querySelector('[data-on-request-list]');
          if (!holder) return;
          holder.replaceChildren(...onRequest.map((source, position) => {
            const row = document.createElement('div');
            row.className = 'on-request-row';
            const button = document.createElement('button');
            button.type = 'button';
            button.className = 'action';
            button.textContent = source.kind === 'youtube' ? 'Play from YouTube' : `Play from ${source.label}`;
            const warning = document.createElement('small');
            warning.id = `on-request-warning-${position}`;
            warning.textContent = source.warning || '';
            button.setAttribute('aria-describedby', warning.id);
            button.addEventListener('click', () => {
              let at = sources.findIndex((item) => item.id === source.id);
              if (at < 0) { sources.push(source); at = sources.length - 1; }
              index = at;
              autoWalk = false;
              hideFailure();
              setStatus(`Playing from ${source.label}.`);
              mount();
            });
            row.append(button, warning);
            return row;
          }));
          holder.hidden = !onRequest.length;
        }
        const HOPS = ['Invidious', 'Player page', 'Manifest', 'Segment'];
        let lastReport = null;

        function fillList(list, nodes) { list.replaceChildren(...nodes); }
        function dotted(tag, className) {
          const node = document.createElement(tag);
          node.className = className;
          const dot = document.createElement('span');
          dot.className = 'dot';
          dot.setAttribute('aria-hidden', 'true');
          node.appendChild(dot);
          return node;
        }

        function drawHops(diagnosis) {
          const steps = (diagnosis && diagnosis.steps) || null;
          const firstBad = steps ? steps.findIndex((step) => !step.ok) : -1;
          fillList(part('hops'), HOPS.map((name, hop) => {
            const step = steps ? steps[hop] : null;
            const state = !steps ? 'wait' : !step ? 'skip' : step.ok ? 'ok' : 'bad';
            const item = dotted('li', `hop ${state}`);
            const label = document.createElement('span');
            label.textContent = hop === firstBad && step.detail ? `${name}: ${step.detail}` : name;
            item.appendChild(label);
            if (state === 'skip') item.title = 'Not reached';
            return item;
          }));
        }

        function drawSources(health) {
          fillList(part('sources'), sources.map((source) => {
            const attempt = attempts.get(source.id);
            const info = health[source.id] || {};
            const cooling = Number(info.cooldown_seconds || (info.health || {}).cooldown_seconds || 0);
            const state = attempt && attempt.state === 'failed' ? 'is-bad' : attempt && attempt.state === 'ok' ? 'is-ok' : cooling ? 'is-hold' : '';
            const row = dotted('li', state);
            const name = document.createElement('strong');
            name.textContent = source.label;
            const why = document.createElement('span');
            why.className = 'why';
            why.textContent = attempt && attempt.why ? attempt.why : cooling ? `In cooldown for ${cooling}s` : attempt ? 'Trying' : 'Not tried';
            why.title = why.textContent;
            const ms = document.createElement('span');
            ms.className = 'ms';
            ms.textContent = attempt && attempt.ms >= 100 ? `${(attempt.ms / 1000).toFixed(1)}s` : '';
            row.append(name, why, ms);
            return row;
          }));
        }

        function causeLine() {
          const failed = [...attempts.entries()].filter(([, attempt]) => attempt.state === 'failed');
          if (!failed.length) return stage.dataset.fallbackReason || 'No playback source is enabled in settings.';
          /* The first failure is the cause; later ones are the ladder working through what
             was left. A source this page's own policy refuses never had a chance, so it
             only counts when nothing else failed. */
          const [id, attempt] = failed.find(([, item]) => item.why !== REASONS.csp) || failed[0];
          const source = sources.find((item) => item.id === id);
          return `${source ? source.label : id}: ${attempt.why}`;
        }

        function buildReport(diagnosis, diagnosisError) {
          const lines = ['OpenFeed playback report', `Video: ${VIDEO_ID}`, `Time: ${new Date().toISOString()}`];
          lines.push(`Result: ${part('cause').textContent}`);
          if (diagnosis) {
            lines.push(`Diagnosis: ${diagnosis.code || 'unknown'}`, 'Chain:');
            (diagnosis.steps || []).forEach((step) => lines.push(`  ${step.ok ? 'ok  ' : 'FAIL'} ${step.name}${step.detail ? ` (${step.detail})` : ''}`));
          } else if (diagnosisError) {
            lines.push(`Diagnosis: not available (${diagnosisError})`);
          }
          lines.push('Sources:');
          if (!sources.length) lines.push('  none enabled');
          sources.forEach((source) => {
            const attempt = attempts.get(source.id);
            lines.push(`  ${source.label}: ${attempt ? `${attempt.state}${attempt.why ? `, ${attempt.why}` : ''}${attempt.ms ? `, ${(attempt.ms / 1000).toFixed(1)}s` : ''}` : 'not tried'}`);
          });
          lines.push(`Browser: ${navigator.userAgent}`);
          return lines.join('\\n');
        }

        async function showFailure() {
          settled = true;
          clearTimer();
          setStatus('');
          stage.classList.remove('is-ready');
          stage.classList.add('is-exhausted');
          dropKit();
          if (frame) { frame.remove(); frame = null; }
          seekTarget = null;
          if (note) note.hidden = true;
          postUp({ type: 'openfeed:playback-failed' });

          const nextButton = part('next');
          const upcomingAt = nextPrivate(index);
          const upcoming = upcomingAt >= 0 && upcomingAt !== index ? sources[upcomingAt] : null;
          nextButton.hidden = !upcoming;
          if (upcoming) nextButton.title = `Try ${upcoming.label}`;
          part('cause').textContent = causeLine();
          part('detail').textContent = '';
          fillList(part('remedies'), []);
          drawOnRequest();
          drawHops(null);
          drawSources({});
          card.hidden = false;
          lastReport = buildReport(null, '');

          const walk = parseData(stage.dataset.diagnosis, null)
            ? Promise.resolve(parseData(stage.dataset.diagnosis, null))
            : fetch(`/api/playback/diagnose?video_id=${encodeURIComponent(VIDEO_ID)}`).then((response) => {
              if (!response.ok) throw new Error(`HTTP ${response.status}`);
              return response.json();
            }).then((data) => data.diagnosis);
          const health = fetch('/api/playback/sources')
            .then((response) => (response.ok ? response.json() : { sources: [] }))
            .then((data) => Object.fromEntries((data.sources || []).map((source) => [source.id, source])))
            .catch(() => ({}));
          let diagnosis = null;
          let diagnosisError = '';
          try { diagnosis = await walk; } catch (error) { diagnosisError = error.message || 'request failed'; }
          drawSources(await health);
          if (diagnosis) {
            drawHops(diagnosis);
            /* The hop walk only speaks for Invidious. If Invidious was never tried, the
               cause is still whichever source failed first. */
            if (!diagnosis.ok && attempts.has('invidious')) {
              part('cause').textContent = diagnosis.headline || causeLine();
              part('detail').textContent = diagnosis.explanation || '';
              fillList(part('remedies'), (diagnosis.remedies || []).map((text) => {
                const item = document.createElement('li');
                item.textContent = text;
                return item;
              }));
            } else if (diagnosis.ok) {
              /* The chain is fine, so the hop walk is not the cause: say what actually broke. */
              part('detail').textContent = 'Invidious passed its chain check for this video, so the hops below are not the cause. Each source shows what happened to it.';
            }
          } else {
            part('detail').textContent = `The chain check did not run: ${diagnosisError}.`;
            drawHops({ steps: [] });
          }
          lastReport = buildReport(diagnosis, diagnosisError);
        }

        function hideFailure() {
          card.hidden = true;
          stage.classList.remove('is-exhausted');
          if (spinner) spinner.hidden = false;
        }

        /* A playing source that dies. Invidious may recover by itself, so give it the
           chance; a plain <video> has no such retry and fails at once. */
        function failLive(why, detail, grace, stillBroken) {
          const source = sources[index];
          const finish = () => {
            if (!stillBroken() || !stage.classList.contains('is-ready') || sources[index] !== source) return;
            resumeAt = seekTarget ? Number(seekTarget.currentTime) || 0 : 0;
            report(source, false);
            endAttempt(source, 'failed', `${detail || REASONS[why] || why}, stopped at ${clock(resumeAt)}`);
            autoWalk = false;
            showFailure();
          };
          if (grace) window.setTimeout(finish, grace); else finish();
        }

        /* --- Stall ---------------------------------------------------------------------
           A source can hand over a stream the browser buffers but never plays: no error,
           a spinner at 0:00. Invidious does that for C3wRLV2TGPQ, whose manifest carries a
           dozen dubbed audio tracks, straight from Invidious as much as through us. Once
           playback has been asked for, a clock that has not moved for STALL_DEADLINE says
           so and offers the next source in the ladder. Paused, hidden or ended is not a
           stall. */
        const STALL_DEADLINE = 12000;
        const stall = document.querySelector('[data-stall]');
        let stallWatch = 0;
        function hideStall() { if (stall) stall.hidden = true; }
        function stopStallWatch() {
          if (stallWatch) { window.clearInterval(stallWatch); stallWatch = 0; }
          hideStall();
        }
        function watchStall(video) {
          stopStallWatch();
          if (!stall) return;
          let at = Number(video.currentTime) || 0;
          let since = performance.now();
          stallWatch = window.setInterval(() => {
            if (seekTarget !== video) { stopStallWatch(); return; }
            const now = Number(video.currentTime) || 0;
            const idle = video.paused || video.ended || document.visibilityState === 'hidden';
            if (idle || now !== at || video.readyState >= 3) {
              at = now;
              since = performance.now();
              if (!stall.hidden && !idle) hideStall();
              return;
            }
            /* Before the start deadline settles it, the ladder's own timer is in charge. */
            if (!settled || !stall.hidden || performance.now() - since < STALL_DEADLINE) return;
            showStall(video);
          }, 1000);
        }
        function showStall(video) {
          const source = sources[index];
          const upcomingAt = nextPrivate(index);
          const upcoming = upcomingAt >= 0 && upcomingAt !== index ? sources[upcomingAt] : null;
          const waited = `${Math.round(STALL_DEADLINE / 1000)}s`;
          const why = `Stalled at ${clock(video.currentTime)} for ${waited} with no picture`;
          if (!upcoming) {
            report(source, false);
            endAttempt(source, 'failed', why);
            autoWalk = false;
            showFailure();
            return;
          }
          stall.querySelector('[data-stall-text]').textContent = `${source.label} has not started playing after ${waited}. It may not be able to play this video.`;
          const nextButton = stall.querySelector('[data-stall-next]');
          nextButton.textContent = `Try ${upcoming.label}`;
          nextButton.onclick = () => {
            report(source, false);
            endAttempt(source, 'failed', why);
            resumeAt = Number(video.currentTime) || resumeAt;
            /* Chosen by hand, so a failure comes back here rather than walking on. */
            autoWalk = false;
            index = upcomingAt;
            stopStallWatch();
            setStatus(`Trying ${sources[index].label}.`);
            mount();
          };
          stall.hidden = false;
        }
        document.querySelector('[data-stall-wait]')?.addEventListener('click', () => {
          /* Waiting longer earns another full deadline before the next prompt. */
          hideStall();
          if (seekTarget) watchStall(seekTarget);
        });

        function next(why, detail) {
          if (settled) return;
          clearTimer();
          const failed = sources[index];
          report(failed, false);
          const reason = detail || REASONS[why] || why;
          endAttempt(failed, 'failed', reason);
          /* The automatic walk never reaches a third-party player; those need a press. */
          if (!autoWalk || index + 1 >= sources.length || isOnRequest(sources[index + 1])) { showFailure(); return; }
          index += 1;
          console.info('OpenFeed player: switching source', why, '->', sources[index].id);
          setStatus(`${failed.label}: ${reason}. Trying ${sources[index].label}.`);
          mount();
        }

        /* A source's URL with a new start point, because each backend spells it differently. */
        function urlFrom(source, seconds) {
          if (!seconds) return source.url;
          const url = new URL(source.url, window.location.href);
          url.searchParams.set(source.kind === 'youtube' ? 'start' : 't', String(Math.floor(seconds)));
          return url.href;
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
        let pendingSeek = null;
        /* Captions, Media Session and cast for a same-origin player (assets/player/kit). */
        let kit = null;
        let mediaQueue = {};
        /* Autoplay's on/off, told by the watch page; null until it says, which keeps the
           player bar's switch hidden rather than showing a guess. */
        let autoplayState = null;
        function dropKit() {
          if (!kit) return;
          try { kit.dispose(); } catch (error) { /* the frame is already gone */ }
          kit = null;
        }
        /* What the host page last asked for: 'play', 'pause', or '' when it never said.
           Kept when there is no video yet, so a Short left behind while it was still
           loading stays parked when the video arrives. */
        let hostWants = '';
        function playForHost() {
          seekTarget.muted = false;
          const play = seekTarget.play();
          if (play && play.catch) play.catch((error) => {
            /* Autoplay with sound can be refused; falling back to muted still means the
               picture moves the instant the Short arrives. Only a refusal is retried: a
               pause landing on a pending play() rejects it with AbortError, and that is
               the viewer leaving this Short, not the browser getting in the way. */
            if (error?.name !== 'NotAllowedError' || hostWants !== 'play' || !seekTarget) return;
            seekTarget.muted = true;
            const retry = seekTarget.play();
            if (retry && retry.catch) retry.catch(() => {});
          });
        }
        function seekTo(seconds) {
          seekTarget.currentTime = seconds;
          const play = seekTarget.play();
          if (play && play.catch) play.catch(() => {});
        }
        window.addEventListener('message', (event) => {
          if (event.origin !== window.location.origin) return;
          const data = event.data || {};
          if (data.type !== 'openfeed:seek') return;
          const seconds = Number(data.seconds);
          if (!isFinite(seconds) || seconds < 0) return;
          if (seekTarget) { seekTo(seconds); return; }
          const source = sources[index];
          /* A cross-origin player has no reachable clock, but it can be reopened at a time. */
          if (frame && source && !source.same_origin && settled) { frame.src = urlFrom({ ...source, url: source.url.replace('autoplay=0', 'autoplay=1') }, seconds); return; }
          /* Still loading: hold the seek until there is a video to apply it to. */
          pendingSeek = seconds;
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
          if (type !== 'openfeed:play' && type !== 'openfeed:pause' && type !== 'openfeed:mute' && type !== 'openfeed:toggle') return;
          if (type === 'openfeed:play') hostWants = 'play';
          if (type === 'openfeed:pause') hostWants = 'pause';
          if (!seekTarget) {
            /* A cross-origin player has no video to start, so it reopens with autoplay on. */
            const source = sources[index];
            if (type === 'openfeed:play' && frame && source && !source.same_origin && source.url.includes('autoplay=0')) {
              source.url = source.url.replace('autoplay=0', 'autoplay=1');
              frame.src = urlFrom(source, 0);
            }
            return;
          }
          if (type === 'openfeed:pause') { seekTarget.pause(); return; }
          if (type === 'openfeed:mute') { seekTarget.muted = Boolean(event.data.muted); return; }
          if (type === 'openfeed:toggle') {
            if (!seekTarget.paused) { seekTarget.pause(); return; }
            const resume = seekTarget.play();
            if (resume && resume.catch) resume.catch(() => {});
            return;
          }
          playForHost();
        });
        /* The watch page's c key and its up-next list, for the controls the kit draws. */
        window.addEventListener('message', (event) => {
          if (event.origin !== window.location.origin) return;
          const data = event.data || {};
          if (data.type === 'openfeed:captions-toggle') {
            if (kit) kit.toggleCaptions();
          } else if (data.type === 'openfeed:key') {
            /* A player shortcut pressed while focus was on the watch page. */
            if (kit && typeof data.key === 'string') kit.key(data.key);
          } else if (data.type === 'openfeed:media-queue') {
            mediaQueue = { next: Boolean(data.next), previous: Boolean(data.previous) };
            if (kit) kit.setQueue(mediaQueue);
          } else if (data.type === 'openfeed:autoplay-state') {
            autoplayState = Boolean(data.on);
            if (kit && kit.setAutoplay) kit.setAutoplay(autoplayState);
          }
        });
        postUp({ type: 'openfeed:shell-ready' });

        /* The chrome stylesheet, assets/player/chrome.css. It has to travel into the embed
           document because the embed never loads our CSS, and the embed being same-origin
           is the only reason that is possible: the YouTube embed cannot be restyled at all.

           It replaces video.js's solid bar and title card with one flat scrim and an Ember
           scrubber, hides Invidious's bright segment markers, restores the 16:9 ratio that
           Invidious's 82vh fluid sizing breaks, and carries the rules for the SponsorBlock
           marks and skip button, which are created inside the embed too. */
        const EMBED_CSS = `__OPENFEED_PLAYER_CHROME__`;

        function styleEmbed(doc) {
          if (!doc || doc.getElementById('openfeed-embed-style')) return;
          const style = doc.createElement('style');
          style.id = 'openfeed-embed-style';
          style.textContent = EMBED_CSS;
          (doc.head || doc.documentElement).appendChild(style);
          if (IS_SHORT) doc.documentElement.classList.add('of-short');
        }

        /* video.js writes title attributes, which would sit on top of the styled tooltip
           after a second's hover. Moved to data-tip, and kept moved as labels change. */
        function tipify(doc) {
          const bar = doc.querySelector('.vjs-control-bar');
          if (!bar) return;
          const sync = () => bar.querySelectorAll('button[title]').forEach((button) => {
            button.dataset.tip = button.title === 'Playback Rate' ? 'Speed' : button.title;
            button.removeAttribute('title');
          });
          bar.querySelector(':scope > .vjs-http-source-selector > button')?.setAttribute('title', 'Quality');
          sync();
          new doc.defaultView.MutationObserver(sync).observe(bar, { subtree: true, attributes: true, attributeFilter: ['title'] });
          /* video.js steps to the next rate on a press of the speed button itself. On a
             phone that meant one tap per step; with a mouse, the row opens on hover, so
             the press meant to pick from it had already set 1.25x. The button only opens
             and closes the row, in both bars. */
          doc.querySelectorAll('.vjs-playback-rate').forEach((rate) => {
            const menu = rate.querySelector('.vjs-menu');
            const button = rate.querySelector(':scope > button');
            if (!menu || !button) return;
            const setOpen = (open) => {
              menu.classList.toggle('vjs-lock-showing', open);
              button.setAttribute('aria-expanded', open ? 'true' : 'false');
            };
            /* Caught on the way down, because video.js steps the rate on its own tap event. */
            ['touchstart', 'touchend', 'click'].forEach((name) => rate.addEventListener(name, (event) => {
              if (!button.contains(event.target)) return;
              event.stopPropagation();
              if (name === 'click') setOpen(!menu.classList.contains('vjs-lock-showing'));
            }, true));
            menu.addEventListener('pointerup', () => window.setTimeout(() => setOpen(false), 0));
            menu.addEventListener('keydown', (event) => {
              if (event.key === 'Enter' || event.key === ' ' || event.key === 'Escape') window.setTimeout(() => setOpen(false), 0);
            });
            doc.addEventListener('pointerdown', (event) => { if (!rate.contains(event.target)) setOpen(false); });
          });
        }

        /* Chapters are cut into the scrubber, and the hover tooltip names the one under the
           pointer. Skipped entirely on a video without them. */
        function drawChapters(doc, video) {
          if (!chapters.length) return;
          const duration = Number(video.duration) || 0;
          if (!duration) { video.addEventListener('loadedmetadata', () => drawChapters(doc, video), { once: true }); return; }
          const holder = doc.querySelector('.vjs-progress-holder');
          if (!holder || holder.querySelector('.of-chapter-marks')) return;
          const strip = doc.createElement('div');
          strip.className = 'of-chapter-marks';
          chapters.forEach((chapter) => {
            if (!chapter.start || chapter.start >= duration) return;
            const tick = doc.createElement('span');
            tick.className = 'of-chapter-tick';
            tick.style.left = `${(chapter.start / duration) * 100}%`;
            strip.appendChild(tick);
          });
          holder.appendChild(strip);
          const tip = doc.querySelector('.vjs-progress-holder .vjs-mouse-display .vjs-time-tooltip');
          if (!tip) return;
          holder.addEventListener('mousemove', (event) => {
            const box = holder.getBoundingClientRect();
            const at = ((event.clientX - box.left) / box.width) * duration;
            const current = chapters.filter((chapter) => chapter.start <= at).pop();
            tip.dataset.chapter = current ? current.title : '';
          });
        }

        /* The page cannot read a player's clock, so the player reads it out. The chapter
           highlight and the Shorts progress line both run from this. */
        function reportClock(video) {
          let last = 0;
          const tick = (event) => {
            const now = performance.now();
            if (event.type === 'timeupdate' && now - last < 400) return;
            last = now;
            postUp({ type: 'openfeed:clock', seconds: Number(video.currentTime) || 0, duration: Number(video.duration) || 0, paused: video.paused, muted: video.muted });
          };
          ['timeupdate', 'play', 'pause', 'seeked', 'volumechange', 'loadedmetadata'].forEach((name) => video.addEventListener(name, tick));
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
          tipify(doc);
          trackPosition(video);
          reportClock(video);
          sbAttach(doc, video);
          drawChapters(doc, video);
          dropKit();
          if (!IS_SHORT && window.OpenFeedPlayerKit) {
            try {
              kit = window.OpenFeedPlayerKit.attach({
                doc, video, videoId: VIDEO_ID, queue: mediaQueue, post: postUp,
                autoplay: autoplayState, speed: Number(stage.dataset.defaultSpeed) || 1,
                meta: { title: stage.dataset.title || '', channel: stage.dataset.channel || '' },
              });
            } catch (error) { console.info('OpenFeed player: media controls unavailable', error); kit = null; }
          }
          if (pendingSeek !== null) { seekTo(pendingSeek); pendingSeek = null; }
          if (hostWants === 'pause') video.pause();
          else if (hostWants === 'play') playForHost();
          /* The embed's own scripts can call play() too, so a parked Short stops at the
             event rather than at the next clock tick. */
          video.addEventListener('play', () => { if (hostWants === 'pause') video.pause(); });
          const ready = () => markReady(sources[index]);
          video.addEventListener('playing', ready, { once: true });
          video.addEventListener('timeupdate', ready, { once: true });
          video.addEventListener('loadeddata', ready, { once: true });

          /* video.js holds the readable error; a bare <video> only has its MediaError. */
          let player = null;
          try {
            const library = frame.contentWindow.videojs;
            player = library ? Object.values(library.getPlayers()).find(Boolean) : null;
          } catch (error) { player = null; }
          const errorText = () => {
            const failure = (player && player.error()) || video.error;
            return failure ? `Media error ${failure.code}${failure.message ? `: ${failure.message}` : ''}` : '';
          };
          const broken = () => Boolean((player && player.error()) || video.error);
          const onMediaError = () => {
            if (!settled) { next('media error', errorText()); return; }
            failLive('media error', errorText(), player ? RECOVERY_GRACE : 0, broken);
          };
          video.addEventListener('error', onMediaError);
          if (player) player.on('error', () => { if (settled) onMediaError(); });
          watchStall(video);
          clearTimer();
          timer = window.setTimeout(() => {
            /* Paused with no error means autoplay was refused or this is a preload, not that
               the source is broken: hand over to the player and its play button. A press
               that then fails comes back through the media error path above. */
            if (video.paused && !broken()) { markReady(sources[index], true); return; }
            next('never started', errorText());
          }, START_DEADLINE);
          return true;
        }

        function mount() {
          const source = sources[index];
          settled = false;
          seekTarget = null;
          beginAttempt(source);
          dropKit();
          if (frame) frame.remove();
          if (spinner) spinner.hidden = false;
          stage.classList.remove('is-ready');
          frame = document.createElement('iframe');
          frame.title = `${source.label} player`;
          frame.allow = 'autoplay; fullscreen; picture-in-picture; encrypted-media';
          /* The page sends no Referer to other sites. YouTube's embed refuses to play
             without one, so it alone gets this site's origin (never the page path); the
             opt-in in Settings says so. */
          if (!source.same_origin) frame.referrerPolicy = source.kind === 'youtube' ? 'strict-origin' : 'no-referrer';
          frame.setAttribute('allowfullscreen', '');
          frame.addEventListener('error', () => next('frame error'));
          frame.addEventListener('load', () => {
            if (source.same_origin) {
              /* An unreadable same-origin frame is a browser error page, not a player. */
              if (!watchSameOrigin()) next('frame error');
            } else {
              /* Cross-origin players cannot be inspected; a clean load is the signal. */
              markReady(source);
            }
          });
          const wanted = hostWants === 'play' && !source.same_origin;
          frame.src = urlFrom(wanted ? { ...source, url: source.url.replace('autoplay=0', 'autoplay=1') } : source, resumeAt);
          stage.appendChild(frame);
          clearTimer();
          timer = window.setTimeout(() => next('load timeout'), LOAD_DEADLINE);
        }

        /* A frame the page's own policy refuses still fires load, on an error document,
           which used to count as a healthy start. The violation is the real signal. */
        document.addEventListener('securitypolicyviolation', (event) => {
          if (!frame || !String(event.effectiveDirective || event.violatedDirective).startsWith('frame-src')) return;
          const source = sources[index];
          if (!source || source.same_origin) return;
          reported.delete(`${source.id}:true`);
          settled = false;
          stage.classList.remove('is-ready');
          next('csp');
        });

        const retry = document.querySelector('[data-retry-preferred]');
        const invidiousIndex = sources.findIndex((source) => source.id === 'invidious');
        if (retry && invidiousIndex < 0) retry.hidden = true;
        retry?.addEventListener('click', () => {
          index = Math.max(0, invidiousIndex);
          if (note) note.hidden = true;
          mount();
        });

        part('retry').addEventListener('click', () => window.location.reload());
        part('next').addEventListener('click', () => {
          /* Chosen by hand, so a failure comes back here rather than walking on. */
          autoWalk = false;
          const upcomingAt = nextPrivate(index);
          if (upcomingAt < 0) return;
          index = upcomingAt;
          hideFailure();
          setStatus(`Trying ${sources[index].label}.`);
          mount();
        });
        part('diagnostics').addEventListener('click', () => {
          if (window.parent === window) window.location.href = '/?settings=playback';
          else postUp({ type: 'openfeed:open-diagnostics' });
        });
        part('copy').addEventListener('click', async (event) => {
          const button = event.currentTarget;
          const text = lastReport || '';
          let copied = false;
          try {
            await navigator.clipboard.writeText(text);
            copied = true;
          } catch (error) {
            /* navigator.clipboard needs https, and plenty of instances run on a LAN address. */
            const area = document.createElement('textarea');
            area.value = text;
            area.style.position = 'fixed';
            area.style.opacity = '0';
            document.body.appendChild(area);
            area.select();
            try { copied = document.execCommand('copy'); } catch (fallback) { copied = false; }
            area.remove();
          }
          button.textContent = copied ? 'Copied' : 'Copy failed';
          window.setTimeout(() => { button.textContent = 'Copy report'; }, 1600);
        });

        if (sources.length) mount(); else showFailure();
      })();
    '''.replace("__OPENFEED_PLAYER_CHROME__", _player_chrome_css())


@app.api_route("/embed/api/v1/storyboards/{path:path}", methods=["GET", "HEAD"])
def proxy_invidious_embed_storyboards(path: str, request: Request):
    """The scrub-preview track, which the player asks for relative to /embed/.

    Invidious answers that spelling with a redirect to /, so every mounted player also
    rendered the whole Home page in the background. On /shorts that is three renders, and
    a tap on Home waited behind them for 12 to 16 seconds.
    """
    return _storyboard_track(request, path)


def _storyboard_track(request: Request, path: str):
    """The scrub-preview track, or an empty one when the video has none at that size.

    Invidious answers 404 when YouTube made no storyboard at the height the player asks
    for (Shorts and fresh uploads have only a tiny one, or none). The player cannot be
    told not to ask, so every such page logged a failed request. An empty WebVTT file is
    the same answer in the player's own terms: no previews, and a clean console.
    """
    response = proxy_invidious(request, f"api/v1/storyboards/{path}", answer_missing_now=True)
    # Only a track request (?height= or ?width=); the bare listing stays a plain 404.
    wants_track = bool(request.query_params.get("height") or request.query_params.get("width"))
    if wants_track and getattr(response, "status_code", 200) == 404:
        return Response(
            "WEBVTT\n" if request.method != "HEAD" else b"",
            media_type="text/vtt",
            headers={"cache-control": "public, max-age=3600"},
        )
    return response


@app.api_route("/embed/{path:path}", methods=["GET", "HEAD"])
def proxy_invidious_embed(path: str, request: Request):
    return proxy_invidious(request, f"embed/{path}")


@app.api_route("/companion/{path:path}", methods=["GET", "HEAD"])
def proxy_invidious_companion(path: str, request: Request):
    return proxy_invidious(request, f"companion/{path}", require_media=path.startswith("api/manifest/dash/"))


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
        logger.info("ggpht proxy fetch failed: %s", exc)
        return Response(status_code=502, headers={"cache-control": "no-store"}, content=_safe_exc_name(exc))

    if status_code != 200 or not content_type.startswith("image/") or (request.method != "HEAD" and not body):
        return Response(status_code=502, headers={"cache-control": "no-store"})
    return Response(
        content=body,
        media_type=content_type.split(";")[0],
        headers={"cache-control": "public, max-age=86400, stale-while-revalidate=604800"},
    )


@app.api_route("/vi/{path:path}", methods=["GET", "HEAD"])
def proxy_invidious_vi(path: str, request: Request):
    return proxy_invidious(request, f"vi/{path}", answer_missing_now=True)


@app.api_route("/api/v1/storyboards/{path:path}", methods=["GET", "HEAD"])
def proxy_invidious_storyboards(path: str, request: Request):
    return _storyboard_track(request, path)


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
    if not get_settings().sign_in_enabled:
        # Sign-in is off: there is nothing to sign in to, so go where they were headed.
        return RedirectResponse(signin.safe_next(next), status_code=status.HTTP_303_SEE_OTHER)
    canonical_redirect = _canonical_login_redirect(request, next)
    if canonical_redirect:
        return canonical_redirect
    # The sign-in page lists every available method (account_pages.sign_in_page). With
    # one OIDC provider and nothing else, /auth/login goes straight to it, as it always has.
    if account_pages.sign_in_page_needed():
        return account_pages.sign_in_page(request, next)
    return oidc.start(request, None, next)


@app.get("/auth/oidc/start")
def auth_oidc_start(request: Request, next: str = "/", intent: str = "signin") -> Response:
    """Start the first configured provider (older links; buttons use /auth/oidc/<name>/start)."""
    return oidc.start(request, None, next, intent)


@app.get("/auth/callback")
def auth_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
    iss: str | None = None,
) -> Response:
    """The callback registered for the original single provider (oidc.py does the work)."""
    return oidc.finish(request, None, code, state, error, error_description, iss)


@app.api_route("/auth/logout", methods=["GET", "POST"])
def auth_logout(request: Request, next: str = "/") -> Response:
    # Signed in by a forward-auth proxy: sign out there too, or it signs you straight back in.
    target = trusted_header.logout_target(request) or _safe_next_path(next)
    response = RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)
    # Ends the server session too, so the cookie is worthless even if it was copied.
    signin.end_session(request, response)
    _clear_auth_session_cookie(response)
    _clear_auth_state_cookie(response)
    # The next sign-in on this browser is asked "Who's watching" again.
    response.delete_cookie(PROFILE_COOKIE, path="/")
    return response


_SEARCH_TYPES = (("all", "All"), ("videos", "Videos"), ("channels", "Channels"), ("playlists", "Playlists"), ("shorts", "Shorts"))
# YouTube still sorts by views. It ignores upload date and rating (see
# discovery._SEARCH_SORT_PARAMS), so for those OpenFeed orders each page it gets back and
# the library matches itself, and the page says that is what happened.
_SEARCH_SORTS = [("relevance", "Relevance"), ("upload_date", "Upload date"), ("view_count", "View count"), ("rating", "Rating")]
_SEARCH_DATES = [("", "Any time"), ("hour", "Last hour"), ("today", "Today"), ("week", "This week"), ("month", "This month"), ("year", "This year")]
_SEARCH_DURATIONS = [("", "Any length"), ("short", "Under 4 min"), ("medium", "4 to 20 min"), ("long", "Over 20 min")]
_SEARCH_FEATURES = [("", "Any feature"), ("live", "Live"), ("4k", "4K"), ("hd", "HD"), ("subtitles", "Subtitles/CC"),
                    ("creative_commons", "Creative Commons"), ("360", "360\u00b0"), ("vr180", "VR180"), ("3d", "3D"), ("hdr", "HDR")]
_SEARCH_CHANNEL_ROWS = 2


def _search_filters(kind: str, sort: str, date: str, duration: str, features: str = "") -> dict[str, str]:
    """Reduce the query string to values that mean something for this result type."""
    kind_key = kind if kind in dict(_SEARCH_TYPES) else "all"
    videos = kind_key in {"all", "videos", "shorts"}
    return {
        "kind": kind_key,
        "sort": sort if videos and sort in dict(_SEARCH_SORTS) else "relevance",
        "date": date if videos and date in dict(_SEARCH_DATES) else "",
        # Shorts are already the shortest band, so a length filter has nothing left to do.
        "duration": duration if kind_key in {"all", "videos"} and duration in dict(_SEARCH_DURATIONS) else "",
        "features": features if kind_key in {"all", "videos"} and features in dict(_SEARCH_FEATURES) else "",
        # The requested values, valid or not for this type. A URL can carry filters the type
        # ignores (an old link, a hand edit); they still count for "Clear filters".
        "raw": {
            "sort": sort if sort in dict(_SEARCH_SORTS) else "relevance",
            "date": date if date in dict(_SEARCH_DATES) else "",
            "duration": duration if duration in dict(_SEARCH_DURATIONS) else "",
            "features": features if features in dict(_SEARCH_FEATURES) else "",
        },
    }


def _search_filtered(filters: dict[str, str]) -> bool:
    return filters["sort"] != "relevance" or bool(filters["date"] or filters["duration"] or filters.get("features"))


def _search_any_filter_in_url(filters: dict[str, Any]) -> bool:
    """Any filter in the URL, applied or not: what decides whether Clear filters shows."""
    raw = filters.get("raw") or {}
    return _search_filtered(filters) or raw.get("sort", "relevance") != "relevance" or any(raw.get(key) for key in ("date", "duration", "features"))


def _search_href(query: str, kind: str = "all", sort: str = "relevance", date: str = "", duration: str = "", features: str = "") -> str:
    """A raw search URL. Escape it at the point of use."""
    params: dict[str, str] = {"q": query}
    if kind != "all":
        params["type"] = kind
    if sort != "relevance":
        params["sort"] = sort
    if date:
        params["date"] = date
    if duration:
        params["duration"] = duration
    if features:
        params["features"] = features
    return f"/search?{urllib.parse.urlencode(params)}"


def _search_controls(query: str, filters: dict[str, str], rating: bool = False) -> str:
    """Type chips, then the filters that apply to the selected type. Every choice is a link,
    so a filtered search is a URL that can be shared or bookmarked.

    `rating` offers the Rating sort, which exists only where Return YouTube Dislike supplies
    dislike counts (search_extras.rating_available)."""
    kind, sort, date, duration = filters["kind"], filters["sort"], filters["date"], filters["duration"]
    features = filters.get("features", "")
    raw = filters.get("raw") or {}

    def chip_href(key: str) -> str:
        # A type chip keeps only the filters that type uses: Shorts keeps sort and date,
        # Channels and Playlists keep none. Carrying the rest left invisible filters in the
        # URL that no control showed and only a hand edit could remove.
        kept = _search_filters(key, raw.get("sort", sort), raw.get("date", date), raw.get("duration", duration), raw.get("features", features))
        return _search_href(query, key, kept["sort"], kept["date"], kept["duration"], kept["features"])

    chips = "".join(
        f'<a class="ch-chip{" active" if key == kind else ""}" href="{escape(chip_href(key), quote=True)}"'
        f'{" aria-current=\"page\"" if key == kind else ""}>{label}</a>'
        for key, label in _SEARCH_TYPES
    )
    menus = ""
    if kind in {"all", "videos", "shorts"}:
        sorts = _SEARCH_SORTS if rating else [item for item in _SEARCH_SORTS if item[0] != "rating"]
        menus += _filter_menu("Sort", sorts, sort, lambda value: _search_href(query, kind, value, date, duration, features))
        menus += _filter_menu("Date", _SEARCH_DATES, date, lambda value: _search_href(query, kind, sort, value, duration, features))
    if kind in {"all", "videos"}:
        menus += _filter_menu("Length", _SEARCH_DURATIONS, duration, lambda value: _search_href(query, kind, sort, date, value, features))
        menus += _filter_menu("Features", _SEARCH_FEATURES, features, lambda value: _search_href(query, kind, sort, date, duration, value))
    if _search_any_filter_in_url(filters):
        menus += f'<a class="btn btn-ghost btn-sm" href="{escape(_search_href(query, kind), quote=True)}" data-clear-search-filters>Clear filters</a>'
    menus_html = f'<div class="search-filters">{menus}</div>' if menus else ""
    return f'<div class="search-toolbar"><nav class="search-types" aria-label="Result type">{chips}</nav>{menus_html}</div>'


def _search_sort_note(filters: dict[str, str]) -> str:
    """Says how a sort YouTube no longer performs was done instead."""
    if filters["sort"] == "upload_date":
        text = ("YouTube no longer sorts search by upload date, so OpenFeed asks for the best matches from today, "
                "this week, this month and this year and lists them newest first, your library matches among them.")
    elif filters["sort"] == "rating":
        text = ("Rated by likes against Return YouTube Dislike's dislike estimate. Videos it has no estimate for yet, "
                "which includes most new results from YouTube, follow in their usual order.")
    else:
        return ""
    return f'<p class="search-note">{escape(text)}</p>'


def _search_user_key(request: Request | None) -> str:
    """The caller, or "" when it cannot be read; the extras that need it then stay off."""
    try:
        return _user_key(request)
    except Exception:  # noqa: BLE001 - search itself works without a user
        return ""


def _search_rating_gate(filters: dict[str, Any], user_key: str) -> bool:
    """Whether Rating can be offered. Without it a rating link falls back to relevance; the
    URL keeps it in `raw`, so Clear filters still shows."""
    available = bool(user_key) and search_extras.rating_available(user_key)
    if filters.get("sort") == "rating" and not available:
        filters["sort"] = "relevance"
    return available


def _order_by_rating(videos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    try:
        ratings = search_extras.stored_ratings([str(video.get("youtube_video_id") or "") for video in videos])
    except Exception as exc:  # noqa: BLE001 - the page still renders, in its own order
        logger.info("ratings unavailable: %s", exc)
        return videos
    return search_extras.order_by_rating(videos, ratings)


def _search_playlist_hits(query: str, user_key: str, filters: dict[str, Any]) -> list[dict[str, Any]]:
    """Matches in the caller's own playlists, for the "In your playlists" block on page 1."""
    if filters["kind"] not in {"all", "videos"} or filters["date"] or filters["duration"] or filters.get("features"):
        return []
    try:
        return search_extras.playlist_matches(query, user_key)
    except Exception as exc:  # noqa: BLE001 - a block that cannot load is simply absent
        logger.info("playlist matches unavailable: %s", exc)
        return []


def _search_playlist_hits_html(hits: list[dict[str, Any]], settings: Any, user_key: str) -> str:
    if not hits:
        return ""
    rows = "".join(
        _media_row(video, settings, variant="library", note=search_extras.playlist_note(video.get("playlist_names")))
        for video in _with_progress(hits, user_key)
    )
    return (
        f'<section class="search-playlist-hits" aria-labelledby="search-playlist-hits-title" data-search-playlist-hits>'
        f'<h2 class="search-section-title" id="search-playlist-hits-title">In your playlists</h2>'
        f'<div class="library-list">{rows}</div></section>'
    )


def _published_key(video: dict[str, Any]) -> float:
    value = video.get("published_at")
    if isinstance(value, datetime):
        stamp = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return stamp.timestamp()
    return float("-inf")


def _order_search_page(videos: list[dict[str, Any]], sort: str) -> list[dict[str, Any]]:
    """Orders that YouTube applies itself are left alone; upload date is done here."""
    if sort == "upload_date":
        return sorted(videos, key=_published_key, reverse=True)
    if sort == "view_count":
        return sorted(videos, key=lambda video: int(video.get("view_count") or 0), reverse=True)
    return videos


def _search_suggestions(query: str, user_key: str = "") -> list[str]:
    """Other searches worth trying, all built from what was typed.

    Fewer words first, then the closest name among your subscriptions, which turns a
    misspelled channel into the channel.
    """
    words = query.split()
    found: list[str] = []
    if len(words) > 1:
        found += [" ".join(words[:-1]), " ".join(words[1:])]
        found += sorted(words, key=len, reverse=True)[:2]
    try:
        subscriptions = db.list_subscriptions(500, user_key=user_key) if user_key else []
        titles = {str(item.get("title") or "").lower(): str(item.get("title") or "") for item in subscriptions}
        found += [titles[match] for match in difflib.get_close_matches(query.lower(), titles.keys(), n=2, cutoff=0.6)]
    except Exception as exc:
        logger.info("search suggestions skipped: %s", exc)
    seen = {query.lower()}
    unique = []
    for item in found:
        if item and item.lower() not in seen:
            seen.add(item.lower())
            unique.append(item)
    return unique[:4]


def _search_empty(query: str, filters: dict[str, str], user_key: str = "") -> str:
    kind = filters["kind"]
    filtered = _search_filtered(filters)
    pills = []
    if filtered:
        pills.append(f'<a class="ch-chip" href="{escape(_search_href(query, kind), quote=True)}">Clear filters</a>')
    if kind != "all":
        pills.append(f'<a class="ch-chip" href="{escape(_search_href(query), quote=True)}">Search everything</a>')
    pills += [
        f'<a class="ch-chip" href="{escape(_search_href(text), quote=True)}">{escape(text)}</a>'
        for text in _search_suggestions(query, user_key)
    ]
    action = f'<div class="search-suggestions">{"".join(pills)}</div>' if pills else ""
    body = "Filters narrow this search, so try clearing them." if filtered else "Check the spelling, or try fewer or different words."
    return _empty_state("search", "Nothing matched this search", body, action)


def _search_channels(query: str, limit: int, page: int = 1, user_key: str = "") -> tuple[list[dict[str, Any]], str, bool]:
    """Channels for a search: yours first, then YouTube's. (rows, error, youtube had any).

    Subscribe state on each row is `user_key`'s; with no user it reads as unsubscribed.

    A channel found on YouTube is stored before it is linked, because /channel/ reads
    the database and would otherwise answer 404.
    """
    local: list[dict[str, Any]] = []
    if page == 1:
        try:
            local = db.search_channels(query, limit, user_key=user_key)
        except Exception as exc:
            logger.warning("channel search failed for %s: %s", privacy.loggable(query), exc)
    seen = {str(row.get("youtube_channel_id") or "") for row in local}
    remote: list[dict[str, Any]] = []
    error = ""
    try:
        remote = discovery.search_youtube_channels(query, page=page)
    except Exception as exc:
        error = _safe_exc_name(exc)
        logger.warning("youtube channel search failed for %s: %s", privacy.loggable(query), exc)
    fresh = [item for item in remote if item.get("youtube_channel_id") not in seen][: max(0, limit - len(local))]
    stored: list[dict[str, Any]] = []
    if fresh:
        try:
            stored = db.store_search_channels(fresh, user_key=user_key)
        except Exception as exc:
            logger.warning("could not store channels for %s: %s", privacy.loggable(query), exc)
    return (local + stored)[:limit], error, bool(remote)


def _notification_levels(user_key: str) -> dict[str, str]:
    try:
        return db.channel_notification_levels(user_key)
    except Exception as exc:
        logger.info("notification levels unavailable: %s", exc)
        return {}


def _channel_results_html(channels: list[dict[str, Any]], user_key: str) -> str:
    levels = _notification_levels(user_key) if channels else {}
    return "".join(
        _channel_card(channel, levels.get(str(channel.get("youtube_channel_id") or ""), "all"))
        for channel in channels
    )


def _playlist_row(playlist_id: str, title_text: str, href: str, meta: str, thumbnail: str = "", external: bool = False) -> str:
    """One playlist as a media row. href and thumbnail are raw; they are escaped here."""
    title = escape(title_text)
    attrs = ' target="_blank" rel="noopener noreferrer"' if external else ""
    link = escape(href, quote=True)
    image = (
        f'<span class="thumb-fallback">{escape(_avatar_initials(title_text))}</span>'
        f'<img src="{escape(thumbnail, quote=True)}" alt="" loading="lazy" onerror="this.remove()">'
        if thumbnail else f'<span class="playlist-cover-empty">{_icon("playlist")}</span>'
    )
    return f'''
    <article class="media-row" data-result-id="{escape(playlist_id, quote=True)}">
      <a class="thumb" href="{link}"{attrs} aria-label="Open {escape(title_text, quote=True)}">{image}</a>
      <div class="media-row-copy">
        <h2><a href="{link}"{attrs}>{title}</a></h2>
        <p>{escape(meta)}</p>
      </div>
    </article>
    '''


def _youtube_playlist_row(playlist: dict[str, Any]) -> str:
    """A YouTube playlist from search. It opens in OpenFeed's remote playlist page
    (`/playlist?list=`), which can play it and save it to the library.

    The cover goes through the local thumbnail proxy or not at all, so a result row
    never sends the viewer's browser to Google.
    """
    playlist_id = str(playlist.get("playlist_id") or "")
    quoted = urllib.parse.quote(playlist_id, safe="")
    in_app = channel_tools.is_remote_playlist_id(playlist_id)
    if in_app:
        href = f"/playlist?list={quoted}"
    else:
        base = str(getattr(get_settings(), "invidious_public_url", "") or "").rstrip("/")
        href = f"{base}/playlist?list={quoted}" if base else f"https://www.youtube.com/playlist?list={quoted}"
    cover = re.search(r"/vi/([\w-]+)/", str(playlist.get("thumbnail_url") or ""))
    thumbnail = f"/vi/{cover.group(1)}/hqdefault.jpg" if cover else ""
    count = playlist.get("video_count")
    parts = [str(playlist.get("author") or "")]
    if count:
        parts.append(f"{count} video{'' if count == 1 else 's'}")
    if not in_app:
        parts.append("Opens outside OpenFeed")
    return _playlist_row(playlist_id, str(playlist.get("title") or "Playlist"), href, " · ".join(part for part in parts if part), thumbnail, external=not in_app)


def _local_playlist_row(playlist: dict[str, Any]) -> str:
    count = int(playlist.get("video_count") or 0)
    playlist_id = str(playlist.get("id") or "")
    return _playlist_row(
        playlist_id, str(playlist.get("name") or "Untitled playlist"), f"/playlist/{playlist_id}",
        f"Your playlist · {count} video{'' if count == 1 else 's'}",
        _thumbnail_for({"youtube_video_id": playlist.get("cover_youtube_video_id"), "thumbnail_url": playlist.get("cover_thumbnail_url")}),
    )


def _search_playlists(query: str, user_key: str, page: int = 1) -> tuple[str, int, str, bool]:
    """Rendered playlist rows, yours first. (html, count, error, youtube had any)."""
    local: list[dict[str, Any]] = []
    if page == 1:
        needle = query.lower()
        local = [item for item in db.list_playlists(user_key) if needle in str(item.get("name") or "").lower()]
    remote: list[dict[str, Any]] = []
    error = ""
    try:
        remote = discovery.search_youtube_playlists(query, page=page)
    except Exception as exc:
        error = _safe_exc_name(exc)
        logger.warning("youtube playlist search failed for %s: %s", privacy.loggable(query), exc)
    html = "".join(_local_playlist_row(item) for item in local) + "".join(_youtube_playlist_row(item) for item in remote)
    return html, len(local) + len(remote), error, bool(remote)


def _search_youtube_videos(query: str, page: int, filters: dict[str, str]) -> tuple[list[dict[str, Any]], int]:
    """One page of YouTube videos under the filters. (matching rows, rows scanned)."""
    kind = filters["kind"]
    duration = "short" if kind == "shorts" else filters["duration"]
    if filters["sort"] == "upload_date":
        # YouTube ignores sort=date; its date filters work, so the windows are merged by date.
        remote = search_extras.search_newest_first(query, page, filters["date"], duration, filters.get("features", ""))
    else:
        remote = discovery.search_youtube(
            query, page=page, sort=filters["sort"], date=filters["date"],
            duration=duration, features=filters.get("features", ""),
        )
    # YouTube has no Shorts filter, and its short band runs to four minutes.
    if kind == "shorts":
        return [item for item in remote if db.is_short_video(item)], len(remote)
    if kind == "videos":
        return [item for item in remote if not db.is_short_video(item)], len(remote)
    return remote, len(remote)


def _with_progress(videos: list[dict[str, Any]], request: Request | str) -> list[dict[str, Any]]:
    """The user's watched state on rows read without it. Cosmetic, so never fatal.

    Takes the request (or a user key) so resolving the user is inside the guard too.
    """
    try:
        user_key = request if isinstance(request, str) else _user_key(request)
        return db.attach_user_progress(videos, user_key)
    except Exception as exc:  # noqa: BLE001 - cards still render, just without progress
        logger.info("progress not attached: %s", exc)
        return videos


def _note_catalog(request: Request, videos: list[dict[str, Any]], source: str) -> None:
    """Record that the caller was shown these catalog videos (catalog_scope.py). Never
    fatal: without the row the video simply stays out of their other listings."""
    try:
        catalog_scope.record_rows(_user_key(request), videos, source)
    except Exception as exc:  # noqa: BLE001
        logger.info("catalog provenance skipped: %s", exc)


def _search_local_videos(query: str, limit: int, filters: dict[str, str], user_key: str = "local") -> list[dict[str, Any]]:
    # The library stores nothing about 4K, captions, licence or live status, so a
    # feature filter cannot be applied to it. Showing unfiltered rows under "4K" would
    # be wrong; showing none is honest, and YouTube's filtered results still appear.
    if filters.get("features"):
        return []
    kind = filters["kind"] if filters["kind"] in {"videos", "shorts"} else ""
    return db.search_videos(query, limit, sort=filters["sort"], kind=kind, date=filters["date"], duration=filters["duration"], user_key=user_key)


@app.get("/search", response_class=HTMLResponse)
def search_page(request: Request, q: str = "", limit: int = 40, page: int = 1, type: str = "all", sort: str = "relevance", date: str = "", duration: str = "", features: str = ""):
    auth_redirect = _login_required_redirect(request)
    if auth_redirect:
        return auth_redirect
    settings = get_settings()
    cleaned = q.strip()
    capped = max(1, min(limit, 100))
    page_num = max(1, min(int(page or 1), 5))
    filters = _search_filters(type, sort, date, duration, features)
    kind = filters["kind"]
    user_key = _search_user_key(request)
    rating_ok = _search_rating_gate(filters, user_key)

    search_error = ""
    channels_html = ""
    playlist_hits_html = ""
    has_more = False
    if cleaned and kind == "channels":
        found, search_error, has_more = _search_channels(cleaned, capped, page_num, user_key)
        rows = _channel_results_html(found, _user_key(request))
        count = len(found)
    elif cleaned and kind == "playlists":
        rows, count, search_error, has_more = _search_playlists(cleaned, _user_key(request), page_num)
    else:
        local = _search_local_videos(cleaned, capped, filters, user_key) if cleaned else db.list_recent_videos(capped, user_key)

        # Searching only the local library answered "which videos do I already have that match"
        # rather than the question anyone actually asks. Results come from YouTube via
        # Invidious, with anything already in the library shown first because we know more
        # about it — its watch progress, whether the channel is subscribed, and so on.
        remote: list[dict[str, Any]] = []
        scanned = 0
        channels: list[dict[str, Any]] = []
        # Filters describe videos, so a filtered search has no channel block. The two
        # lookups run side by side so the channel block never slows the video results.
        want_channels = bool(cleaned) and kind == "all" and not _search_filtered(filters)
        with ThreadPoolExecutor(max_workers=1) as pool:
            channel_lookup = pool.submit(_search_channels, cleaned, _SEARCH_CHANNEL_ROWS, 1, user_key) if want_channels else None
            if cleaned:
                try:
                    remote, scanned = _search_youtube_videos(cleaned, page_num, filters)
                except Exception as exc:
                    search_error = _safe_exc_name(exc)
                    logger.warning("youtube search failed for %s: %s", privacy.loggable(cleaned), exc)
            if channel_lookup:
                channels = channel_lookup.result()[0]

        known = {str(video.get("youtube_video_id") or "") for video in local}
        fresh = [item for item in remote if item.get("youtube_video_id") not in known]
        if fresh:
            # Stored so the watch page, playlists and feedback all work on a search result the
            # same way they do on anything else — without this a result opened to a 404.
            try:
                db.store_search_results(fresh[:capped], cleaned, user_key)
            except Exception as exc:
                logger.warning("could not store search results for %s: %s", privacy.loggable(cleaned), exc)

        # Each source gets its own cap. The old `capped - len(local)` slice meant a full
        # library page left zero slots for YouTube, so search silently became library-only
        # as the library grew.
        local_shown = local[:capped]
        fresh_shown = fresh[:capped]
        # Saved videos that match lead in their own block, so they are not listed twice.
        playlist_hits = _search_playlist_hits(cleaned, user_key, filters) if cleaned and user_key else []
        if playlist_hits:
            saved_ids = {str(video.get("youtube_video_id") or "") for video in playlist_hits}
            local_shown = [video for video in local_shown if str(video.get("youtube_video_id") or "") not in saved_ids]
            playlist_hits_html = _search_playlist_hits_html(playlist_hits, settings, user_key)
        combined = local_shown + fresh_shown
        if filters["sort"] in {"view_count", "upload_date"}:
            combined = _order_search_page(combined, filters["sort"])
        elif filters["sort"] == "rating":
            combined = _order_by_rating(combined)
        combined = _with_progress(combined, request)
        rows = "".join(
            _media_row(video, settings, "In your library" if video.get("youtube_video_id") in known else "From YouTube")
            for video in combined
        )
        # Page 1 shows every library match, so there is nothing more to load when the
        # query is empty (recent videos) or YouTube could not be reached.
        has_more = bool(cleaned and not search_error and scanned)
        count = len(combined) + len(playlist_hits)
        if channels:
            channels_html = f'<section class="search-channels" aria-label="Channels">{_channel_results_html(channels, _user_key(request))}</section>'
            count += len(channels)

    note = ""
    if not rows and not channels_html and not playlist_hits_html:
        if search_error:
            rows = _empty_state(
                "search", "Search is unavailable", f"YouTube could not be reached: {search_error}.",
                f'<a class="btn" href="{escape(_search_href(cleaned, kind, filters["sort"], filters["date"], filters["duration"], filters["features"]), quote=True)}">Try again</a>',
            )
        elif cleaned:
            rows = _search_empty(cleaned, filters, user_key)
        else:
            rows = _empty_state("search", "Nothing here yet", "Search YouTube above, or run a sync to fill your library.")
    elif search_error:
        note = f'<p class="search-note">Showing your library only. YouTube could not be reached: {escape(search_error)}.</p>'
    if cleaned and filters.get("features") and kind in {"all", "videos"} and not search_error:
        # _search_local_videos returns nothing under a feature filter; say so, or the
        # missing "In your library" rows look like the library has no matches.
        feature_label = dict(_SEARCH_FEATURES).get(filters["features"], filters["features"])
        note += (
            f'<p class="search-note" data-feature-library-note>Your library is left out while the {escape(feature_label)} filter is on: '
            'OpenFeed does not store which videos are live, HD or captioned, so only YouTube results can be filtered by it.</p>'
        )

    scrollable = bool(cleaned and not search_error and has_more)
    skeleton = _skeleton_channel_rows(3) if kind == "channels" else _skeleton_rows(3)
    scroll_html = (
        _feed_loading("Loading more results", "data-search-loading", skeleton) + '\n'
        '      <div class="feed-sentinel" data-search-sentinel aria-hidden="true"></div>'
        if scrollable
        else ""
    )
    if cleaned:
        heading = f'{count}{"+" if scrollable else ""} result{"" if count == 1 else "s"} for "{cleaned}"'
    else:
        heading = "Recent videos"
    # The header's search field is hidden on a phone, where this page is the way in.
    page_form = f'''
      <form class="search-form search-page-form" action="/search" method="get" role="search">
        <input name="q" type="search" value="{escape(cleaned, quote=True)}" placeholder="Search" aria-label="Search">
        <button type="submit" aria-label="Search">{_icon("search")}</button>
      </form>
    '''
    content = f'''
      {page_form}
      <div class="search-head" data-search-head>{_page_head(heading)}</div>
      {_search_controls(cleaned, filters, rating_ok) if cleaned else ""}
      {_search_sort_note(filters) if cleaned and kind in {"all", "videos", "shorts"} else ""}
      {note}
      {channels_html}
      {playlist_hits_html}
      <section class="result-list" data-search-list data-q="{escape(cleaned, quote=True)}"
               data-type="{kind}" data-sort="{filters["sort"]}" data-date="{filters["date"]}" data-duration="{filters["duration"]}" data-features="{filters["features"]}"
               data-count="{count}" data-next-page="2" data-search-ok="{'1' if scrollable else '0'}">{rows}</section>
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
        return _empty_state(
            "subs", "No videos from your subscriptions yet", "Uploads from the channels you follow appear here after the next sync.",
            '<button class="btn" type="button" data-feed-refresh>Sync now</button>',
        )
    blocks = []
    for index, (label, items) in enumerate(groups):
        cards = "".join(_home_video_card(video, position, settings) for position, video in enumerate(items, start=1))
        group_class = "timeline-group" if index == 0 else "timeline-group past"
        blocks.append(
            f'<section class="{group_class}" data-filter-group>'
            f'<h2 class="timeline-date">{escape(label)}</h2>'
            f'<div class="video-grid">{cards}</div></section>'
        )
    return f'<div class="timeline" data-section-kind="subscription">{"".join(blocks)}</div>'


def _takeout_stages(stage: int) -> str:
    """Choose, upload and read, imported. There is no review stage because the server parses
    and stores in one request, so a stage named Review would not be true.

    stage is 1, 2 or 3. The script moves the same classes while an upload runs.
    """
    items = []
    for number, label in enumerate(("Choose", "Upload and read", "Imported"), start=1):
        state = "is-done" if number < stage or stage == 3 else "is-current" if number == stage else ""
        current = ' aria-current="step"' if number == stage else ""
        items.append(
            f'<li class="stage {state}" data-import-stage="{number}"{current}><span class="stage-node">'
            f'<span class="stage-number">{number}</span>{_icon("check")}</span><span>{label}</span></li>'
        )
    return f'<ol class="stages" data-import-stages aria-label="Import progress">{"".join(items)}</ol>'


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
        videos = int(summary.get("watched_videos") or 0)
        channels = int(summary.get("watched_channels") or 0)
        apply_button = (
            f'<button class="btn btn-primary" type="button" data-takeout-apply-subs>Subscribe to {pending} channels</button>'
            if pending else ""
        )
        # Say why the numbers do not add up, rather than leaving it looking like a failure.
        unavailable_note = (
            f'<p class="dialog-note">{unavailable} channel{"" if unavailable == 1 else "s"} in your export no longer '
            f'exist on YouTube, so there is no feed to subscribe to. '
            f'<button class="link-action" type="button" data-takeout-retry-subs>Try them again</button></p>'
        ) if unavailable else ""
        tiles = "".join(
            f'<div class="status-cell"><span class="stat-label">{escape(label)}</span><strong>{value:,}</strong></div>'
            for label, value in (
                ("Videos", videos), ("Channels", channels), ("Subscriptions", int(counts.get("total", 0))),
                ("Searches", int(summary.get("search_terms") or 0)),
            )
        )
        top = [item for item in (summary.get("top_channels") or []) if isinstance(item, dict)][:5]
        top_html = (
            '<ol class="import-top">' + "".join(
                f'<li><span>{escape(str(item.get("title") or "Unknown channel"))}</span><b>{int(item.get("watch_count") or 0):,}</b></li>'
                for item in top
            ) + "</ol>"
        ) if top else ""
        status_html = f"""
          <div class="import-status" data-import-status>
            <div class="set-label-row"><h4 class="set-label">Imported {when}</h4></div>
            <div class="status-counters">{tiles}</div>
            {f'<section class="set-group"><h4 class="set-label">Most watched</h4>{top_html}</section>' if top_html else ""}
            {unavailable_note}
            <div class="settings-pane-actions">
              {apply_button}
              <button class="btn" type="button" data-feed-refresh>Refresh feed</button>
              <button class="btn btn-danger" type="button" data-takeout-delete data-import-videos="{videos}" data-import-channels="{channels}">Undo import</button>
            </div>
            <p class="dialog-note import-note">{_icon("clock")}<span><strong>Your feed takes a while to catch up.</strong>
               Importing tells OpenFeed what you like, but it still has to fetch videos on those
               subjects from the channels you watch, from searches on your top topics, and from
               what is trending. That runs in the background every sync, and new videos need their
               details fetched before topic matching can see them. Expect the feed to keep shifting
               toward your interests over the next few hours. Refresh feed runs a pass immediately.</span></p>
          </div>
        """
    steps = """
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
               <strong>JSON</strong>. This is the step people miss. Takeout defaults to HTML,
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
            <p>Add the <code>.zip</code> exactly as Google gave it to you, with no need to unzip.</p>
          </li>
        </ol>
    """
    return f"""
      <p class="settings-lede">OpenFeed starts out knowing nothing about you. Your Google Takeout export already
         contains years of what you watched, and importing it skips the cold start.</p>
      {_takeout_stages(3 if existing else 1)}
      {status_html}
      <section class="set-group">
        <h4 class="set-label">{"Import a newer archive" if existing else "Choose your archive"}</h4>
        <div class="import-panel">
          <div class="import-drop" data-import-drop tabindex="0" role="button" aria-label="Choose a Takeout archive">
            {_icon("download")}
            <strong>Drop your Takeout .zip here</strong>
            <small>or click to choose a file</small>
            <input type="file" accept=".zip,application/zip" data-import-file hidden>
          </div>
          <div class="import-chosen" data-import-chosen hidden>
            <div class="set-copy"><strong data-import-name></strong><small data-import-size></small></div>
            <div class="set-control">
              <button class="btn btn-ghost" type="button" data-import-clear>Change</button>
              <button class="btn btn-primary" type="button" data-import-start>Import</button>
            </div>
          </div>
          <div class="import-progress" data-import-progress hidden>
            <div class="playback-progress" data-import-bar><span data-import-bar-fill></span></div>
            <p class="dialog-note" role="status" data-import-state>Reading your archive&hellip;</p>
          </div>
          <div class="import-result is-error" role="alert" data-import-result hidden></div>
        </div>
        <p class="set-foot">Your archive is parsed on this server and never leaves it. Only the counts that feed
           ranking are kept: which channels you watch and how often, and the IDs of videos you have already seen.
           The archive itself is not written to disk, and Undo import removes everything it added.</p>
      </section>
      <details class="import-guide"{"" if existing else " open"}>
        <summary>How to export from Google</summary>
        {steps}
      </details>
    """


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


_CHANNEL_SORTS = [("name", "Name"), ("latest", "Latest upload"), ("videos", "Most videos")] + channel_tools.EXTRA_CHANNEL_SORTS


def _channels_view_switch(active: str) -> str:
    """The one control that changes what this page shows. Links, so a view is a URL."""
    segments = "".join(
        f'<a class="segment" href="{href}"{" aria-current=\"page\"" if key == active else ""}>{label}</a>'
        for key, label, href in (
            ("channels", "Channels", "/channels"),
            ("timeline", "Timeline", "/channels?view=timeline"),
            ("collections", "Collections", "/channels?tab=collections"),
        )
    )
    return f'<nav class="segmented" aria-label="Subscriptions view">{segments}</nav>'


@app.get("/channels", response_class=HTMLResponse)
def channels_page(request: Request, limit: int = 200, collection: str = "", view: str = "", tab: str = "", sort: str = "name", group: str = ""):
    """Manage what you follow.

    Opens on the channel list. The sidebar's Subscriptions entry is already the uploads
    feed, so this page answers a different question: which channels, and how they are
    grouped and notified. The timeline stays one switch away.
    """
    auth_redirect = _login_required_redirect(request)
    if auth_redirect:
        return auth_redirect
    if collection and _valid_uuid(collection):
        return RedirectResponse(url=f"/collection/{collection}", status_code=307)
    settings = get_settings()
    user_key = _user_key(request)
    subscriptions = db.list_subscriptions(max(1, min(limit, 500)), user_key=user_key)
    collections = db.list_subscription_collections(user_key)
    membership = db.list_collection_ids_for_subscriptions(user_key)
    uncollected = sum(1 for item in subscriptions if not membership.get(str(item.get("id") or "")))
    active = "collections" if tab == "collections" or view == "collections" else "timeline" if view == "timeline" else "channels"
    switch = _channels_view_switch(active)
    channel_count = len(subscriptions)
    channel_label = f"{channel_count} channel{'' if channel_count == 1 else 's'}"
    no_match = '<div data-filter-empty hidden>{}</div>'

    if active == "collections":
        cards = "".join(_collection_card(item) for item in collections)
        new_button = f'<button class="btn btn-primary" type="button" data-new-collection>{_icon("plus")}<span>New collection</span></button>'
        head = _page_head(
            "Collections",
            f"{len(collections)} · {uncollected} channel{'' if uncollected == 1 else 's'} not in a collection" if collections else "",
            new_button,
        )
        filter_html = (
            f'<label class="toolbar-search">{_icon("search")}<input type="search" placeholder="Filter collections" '
            f'data-filter-input aria-label="Filter collections"></label>'
            if collections else ""
        )
        body = (
            f'<section id="collections"><h2 class="sr-only">Your collections</h2><div class="collection-grid">{cards}</div>'
            f'{no_match.format(_empty_state("search", "No collections match", "Nothing here matches that filter."))}</section>'
            if collections else
            f'<section id="collections">{_empty_state("collections", "No collections yet", "Group channels you subscribe to. A collection gets its own page with a combined feed.")}</section>'
        )
        toolbar = f'<div class="toolbar subs-toolbar">{switch}{filter_html}</div>'
    elif active == "timeline":
        videos = db.list_subscription_recommendations(60, user_key)
        head = _page_head("Subscriptions", f"{channel_label} · {len(videos)} recent video{'' if len(videos) == 1 else 's'}")
        filter_html = (
            f'<label class="toolbar-search">{_icon("search")}<input type="search" placeholder="Filter videos" '
            f'data-filter-input aria-label="Filter videos"></label>'
        ) if videos else ""
        toolbar = f'<div class="toolbar subs-toolbar">{switch}{filter_html}</div>'
        body = (
            f'{_subscription_timeline(videos, settings)}'
            f'{no_match.format(_empty_state("search", "No videos match", "Nothing in this timeline matches that filter."))}'
        )
    else:
        names = {str(item.get("id") or ""): str(item.get("name") or "Untitled collection") for item in collections}
        groups = [("", "All channels"), ("none", "Not in a collection")] + list(names.items())
        group_key = group if group in dict(groups) else ""
        visible = subscriptions
        if group_key == "none":
            visible = [item for item in visible if not membership.get(str(item.get("id") or ""))]
        elif group_key:
            visible = [item for item in visible if group_key in membership.get(str(item.get("id") or ""), [])]
        sort_key = sort if sort in dict(_CHANNEL_SORTS) else "name"
        if sort_key == "latest":
            epoch = datetime.fromtimestamp(0, tz=timezone.utc)
            visible = sorted(visible, key=lambda item: item.get("latest_video_at") or epoch, reverse=True)
        elif sort_key == "videos":
            visible = sorted(visible, key=lambda item: int(item.get("video_count") or 0), reverse=True)
        elif sort_key in dict(channel_tools.EXTRA_CHANNEL_SORTS):
            try:
                visible = channel_tools.sort_subscriptions(visible, sort_key, channel_tools.subscription_stats(user_key))
            except Exception as exc:  # noqa: BLE001 - the list still renders, by name
                logger.info("channel sort %s unavailable: %s", sort_key, exc)
        levels = _notification_levels(user_key)
        visible = _subscriptions_with_bell(visible, levels)
        rows = "".join(
            _subscription_card(
                item,
                notify_level=item["notify_level"],
                collections=[(key, names[key]) for key in membership.get(str(item.get("id") or ""), []) if key in names],
            )
            for item in visible
        )

        def href(sort_value: str, group_value: str) -> str:
            params = {"view": "channels"}
            if sort_value != "name":
                params["sort"] = sort_value
            if group_value:
                params["group"] = group_value
            return f"/channels?{urllib.parse.urlencode(params)}"

        head = _page_head("Subscriptions", channel_label)
        menus = ""
        filter_html = ""
        if subscriptions:
            menus = (
                _filter_menu("Sort", _CHANNEL_SORTS, sort_key, lambda value: href(value, group_key))
                + _filter_menu("Group", groups, group_key, lambda value: href(sort_key, value))
            )
            filter_html = (
                f'<label class="toolbar-search">{_icon("search")}<input type="search" placeholder="Filter channels" '
                f'data-filter-input aria-label="Filter channels"></label>'
            )
        select_toggle = _channels_select_toggle() if rows else ""
        toolbar = f'<div class="toolbar subs-toolbar">{switch}{menus}{filter_html}{select_toggle}</div>'
        if rows:
            toolbar += _channels_select_bar(collections)
            body = (
                f'<div class="channel-grid">{rows}</div>'
                f'{no_match.format(_empty_state("search", "No channels match", "Nothing here matches that filter."))}'
            )
        elif subscriptions:
            body = _empty_state("collections", "No channels in this group", "Add channels to it from the menu on any row.")
        else:
            body = _empty_state(
                "subs", "No subscriptions yet",
                "Subscribe from any channel page, or import your subscriptions from a Google Takeout archive.",
                '<a class="btn" href="/?settings=import">Import from Takeout</a>',
            )

    content = f'''
      <div data-subscription-total="{channel_count}">{head}</div>
      {toolbar}
      {body}
    '''
    # The sidebar has separate entries for these, so the active one has to follow the view.
    nav_key = "collections" if active == "collections" else "channels"
    page_title = "Collections" if active == "collections" else "Subscriptions"
    return _app_shell(page_title, nav_key, content, request=request)


# --- library sections ---------------------------------------------------------------
# Each of these renders exactly one thing and nothing else, so a nav item can point at a
# page that *is* that thing rather than a page that merely contains it.


def _history_day_label(value: Any) -> str:
    """Today, Yesterday, a weekday inside the last week, then the date."""
    if not isinstance(value, datetime):
        return "Earlier"
    date = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    local = date.astimezone()
    delta = (datetime.now().astimezone().date() - local.date()).days
    if delta <= 0:
        return "Today"
    if delta == 1:
        return "Yesterday"
    if delta < 7:
        return local.strftime("%A")
    return local.strftime("%d %b %Y").lstrip("0")


def _watch_state(video: dict[str, Any]) -> tuple[str, float | None, bool]:
    """How far a history row got: (words, fraction for the resume bar, finished).

    Read from the seconds rather than percent_completed, which has held both a 0-1 ratio
    and a 0-100 figure. Words only ever state what playback_progress recorded.
    """
    def seconds(key: str) -> float:
        try:
            return max(0.0, float(video.get(key) or 0))
        except (TypeError, ValueError):
            return 0.0

    if video.get("manually_marked_watched"):
        return "Marked watched", None, True
    if video.get("completed"):
        return "Finished", None, True
    position = seconds("resume_seconds") or seconds("furthest_seconds") or seconds("current_seconds")
    duration = seconds("progress_duration_seconds") or seconds("duration_seconds")
    if position and duration:
        fraction = min(1.0, position / duration)
        if fraction >= 0.95:
            return "Finished", None, True
        return f"Watched {_duration_text(position)} of {_duration_text(duration)}", fraction, False
    if position:
        return f"Watched {_duration_text(position)}", None, False
    if video.get("latest_event_type") == "watched":
        return "Marked watched", None, True
    return "Opened, not played", None, False


def _history_row(video: dict[str, Any], settings: Any) -> str:
    note, fraction, finished = _watch_state(video)
    video_id = escape(str(video.get("youtube_video_id") or ""), quote=True)
    title = str(video.get("title") or "Untitled")
    overlay = ""
    if finished:
        overlay = f'<span class="row-finished" aria-hidden="true">{_icon("check")}</span>'
    elif fraction:
        overlay = f'<span class="progress-rail" aria-hidden="true"><span style="width:{max(2.0, fraction * 100):.0f}%"></span></span>'
    return _media_row(
        video, settings, "History",
        note=note,
        thumb_overlay=overlay,
        trailing=_row_remove(f'data-remove-history="{video_id}"', f"Remove from history: {title}"),
        menu_rows=_menu_row("x", "Remove from history", attrs=f' data-remove-history="{video_id}"', danger=True),
        variant="library",
        filter_text=f"{title} {video.get('channel_title') or video.get('author') or ''}",
    )


def _history_section_html(history: list[dict[str, Any]], settings: Any, next_limit: int = 0, sort: str = "newest", tools: str = "") -> str:
    groups: list[tuple[str, list[dict[str, Any]]]] = []
    for video in history:
        label = _history_day_label(video.get("latest_event_at") or video.get("published_at"))
        if groups and groups[-1][0] == label:
            groups[-1][1].append(video)
        else:
            groups.append((label, [video]))
    if not groups:
        return _empty_state(
            "history", "Nothing watched yet",
            "Videos you open show up here. Stored on this server only.",
            '<a class="btn btn-sm" href="/">Browse home</a>',
        )
    blocks = []
    for label, items in groups:
        rows = "".join(_history_row(video, settings) for video in items)
        blocks.append(
            f'<section class="day-group" data-filter-group data-day-group>'
            f'<h2 class="day-head"><span>{escape(label)}</span>'
            f'<span class="day-count" data-day-count>{_count_text(len(items))}</span></h2>'
            f'<div class="library-list">{rows}</div></section>'
        )
    sort_query = "&amp;sort=oldest" if sort == "oldest" else ""
    more = (
        f'<div class="library-more"><a class="btn btn-sm" href="/history?limit={int(next_limit)}{sort_query}">'
        f'{"Show newer" if sort == "oldest" else "Show older"}</a></div>'
        if next_limit else ""
    )
    return f'{_library_filter("Filter history", tools)}{"".join(blocks)}{_filter_empty("history")}{more}'


# Liked has no count query of its own, and the list is what the count would be taken from.
LIKED_LIMIT = 400


def _liked_count_text(count: int, limit: int = LIKED_LIMIT) -> str:
    """A count read off a capped list, so reaching the cap reads as a floor."""
    return f"{count}+ videos" if count >= limit else _count_text(count)


def _list_runtime(videos: list[dict[str, Any]]) -> str:
    known = [int(video.get("duration_seconds") or 0) for video in videos if video.get("duration_seconds")]
    return _runtime_text(sum(known), len(known), len(videos))


def _playlists_section_html(custom: list[dict[str, Any]], user_key: str, limit: int = 120) -> str:
    liked = db.list_liked_videos(LIKED_LIMIT, user_key)
    kept = db.list_downloads(user_key, "", 500)
    smart = [
        _smart_list_card(
            "Liked videos", "/liked", "like", len(liked),
            " · ".join(part for part in (_liked_count_text(len(liked)), _list_runtime(liked)) if part), liked[0] if liked else None,
        ),
    ]
    if kept:
        kept_size = _bytes_text(sum(int(row.get("size_bytes") or 0) for row in kept))
        smart.append(_smart_list_card(
            "Downloads", "/downloads", "download", len(kept),
            " · ".join(part for part in (_count_text(len(kept)), kept_size) if part), kept[0],
        ))
    cards = "".join(_playlist_card(playlist) for playlist in custom)
    filter_box = _library_filter("Filter playlists") if custom else ""
    if cards:
        grid = f'<h2 class="sr-only">Your playlists</h2><div class="playlist-grid">{"".join(smart)}{_new_playlist_tile()}{cards}</div>{_filter_empty("playlist")}'
    else:
        grid = f'''<h2 class="sr-only">Your playlists</h2><div class="playlist-grid">{"".join(smart)}</div>{_empty_state(
            "playlist", "No playlists yet",
            "Create one here, or choose Save to playlist from any video's menu.",
            '<button class="btn btn-sm" type="button" data-new-playlist>New playlist</button>',
        )}'''
    feeds = db.list_playlist_subscriptions(limit, user_key=user_key)
    feed_section = ""
    if feeds:
        feed_section = f'''
          <section class="section-block">
            <div class="section-heading"><h2>Playlist feeds</h2></div>
            <div class="channel-grid">{"".join(_subscription_card(item) for item in feeds)}</div>
          </section>
        '''
    return f'{filter_box}{grid}{feed_section}'


def _detail_layout(
    title: str, meta: list[str], cover: str, count: int, actions: str, rows: str,
    description: str = "", icon: str = "playlist", filter_label: str = "", noun: str = "",
    tools: str = "", byline: str = "",
) -> str:
    """The list-detail page shared by a playlist and Liked: panel, then filter and rows.

    title, meta and description are plain text and are escaped here. cover is an escaped
    URL. actions and rows are trusted HTML.
    """
    description_html = (
        f'<p class="playlist-description" title="{escape(description, quote=True)}">{escape(description)}</p>'
        if description else ""
    )
    filter_html = _library_filter(filter_label, tools) if filter_label else ""
    empty_html = _filter_empty(noun) if filter_label else ""
    # byline is trusted HTML: who owns a playlist shared from another profile, its visibility.
    byline_html = f'<div class="playlist-byline">{byline}</div>' if byline else ""
    return f'''
      <div class="playlist-detail">
        <aside class="playlist-panel">
          <div class="playlist-stack"><span class="playlist-cover">{_cover_media(cover, count, icon)}</span></div>
          <div class="playlist-panel-copy">
            <h1>{escape(title)}</h1>
            {byline_html}
            <p class="playlist-meta">{escape(" · ".join(part for part in meta if part))}</p>
            {description_html}
          </div>
          <div class="playlist-panel-actions">{actions}</div>
        </aside>
        <section class="playlist-items">{filter_html}{rows}{empty_html}</section>
      </div>
    '''


# How much of a list "Play all" queues. The playlist page shows 300 rows, Liked up to 400.
WATCH_LIST_LIMIT = 400


def _resolve_watch_list(list_key: str, user_key: str) -> dict[str, Any] | None:
    """The list a watch page is playing through: a playlist id, "liked" or "watch-later".

    None for anything that is not one of this user's lists, so a stale or foreign
    ?list= simply plays the video on its own.
    """
    key = (list_key or "").strip()
    if not key:
        return None
    try:
        if key == "liked":
            return {"key": key, "title": "Liked videos", "href": "/liked",
                    "videos": db.list_liked_videos(WATCH_LIST_LIMIT, user_key)}
        if channel_tools.is_remote_playlist_id(key):
            # A YouTube playlist opened in the app (/playlist?list=): read, not stored.
            meta, videos = channel_tools.remote_playlist(key, WATCH_LIST_LIMIT)
            catalog_scope.record_rows(user_key, videos, "playlist")
            return {"key": key, "title": meta["title"], "href": _remote_playlist_href(key), "videos": videos}
        playlist_id = key
        if key == "watch-later":
            match = next((row for row in db.list_playlists(user_key)
                          if str(row.get("name") or "").strip().lower() == "watch later"), None)
            if not match:
                return None
            playlist_id = str(match.get("id") or "")
        if not _valid_uuid(playlist_id):
            return None
        playlist = _viewable_playlist(playlist_id, user_key)
        if not playlist:
            return None
        # A shared playlist plays in its owner's order, and any playlist in its chosen sort.
        videos = db.list_playlist_videos(playlist_id, WATCH_LIST_LIMIT, str(playlist.get("user_key") or user_key))
        return {"key": key, "title": str(playlist.get("name") or "Untitled playlist"), "href": f"/playlist/{playlist_id}",
                "videos": library_tools.sort_playlist_videos(videos, str(playlist.get("sort_key") or "manual"))}
    except Exception as exc:  # noqa: BLE001 - the video still plays without its list
        logger.info("watch list %r unavailable: %s", key, exc)
        return None


def _list_order(videos: list[dict[str, Any]], shuffle: str = "") -> list[dict[str, Any]]:
    """The list's videos in play order: as listed, or shuffled by a seed carried in the URL.

    The seed makes the shuffled order the same on every watch page of one run through the
    list, so Up next and the next-item link agree, and a fresh Shuffle press gets a new one.
    """
    seen: set[str] = set()
    ordered = []
    for video in videos:
        video_id = str(video.get("youtube_video_id") or "")
        if video_id and video_id not in seen:
            seen.add(video_id)
            ordered.append(video)
    if shuffle:
        random.Random(f"openfeed-shuffle:{shuffle}").shuffle(ordered)
    return ordered


def _list_watch_href(video_id: str, list_key: str, shuffle: str = "") -> str:
    """A /watch link that keeps playing through a list. Already escaped for an attribute."""
    query = {"list": list_key, **({"shuffle": shuffle} if shuffle else {})}
    return escape(f"/watch/{urllib.parse.quote(video_id, safe='')}?{urllib.parse.urlencode(query)}", quote=True)


def _play_all_buttons(videos: list[dict[str, Any]], list_key: str) -> str:
    """Play all and Shuffle for a list page. Both open the watch page in list mode."""
    ordered = _list_order(videos)
    if not ordered:
        return ""
    seed = secrets.token_hex(4)
    shuffled = _list_order(videos, seed)
    first = str(ordered[0].get("youtube_video_id") or "")
    shuffled_first = str(shuffled[0].get("youtube_video_id") or "")
    shuffle_button = (
        f'<a class="btn" href="{_list_watch_href(shuffled_first, list_key, seed)}" data-list-shuffle>{_icon("shuffle")}<span>Shuffle</span></a>'
        if len(ordered) > 1 else ""
    )
    return (
        f'<a class="btn btn-primary" href="{_list_watch_href(first, list_key)}" data-list-play>{_icon("play")}<span>Play all</span></a>'
        + shuffle_button
    )


def _watch_list_panel(watch_list: dict[str, Any] | None, youtube_video_id: str, shuffle: str = "") -> str:
    """The list being played, in play order, with this video marked and the next one tagged.

    Empty when there is no list or this video is not on it.
    """
    if not watch_list:
        return ""
    ordered = _list_order(watch_list["videos"], shuffle)
    ids = [str(video.get("youtube_video_id") or "") for video in ordered]
    if youtube_video_id not in ids:
        return ""
    position = ids.index(youtube_video_id)
    total = len(ids)
    key = watch_list["key"]
    rows = []
    for index, video in enumerate(ordered):
        video_id = ids[index]
        title_text = str(video.get("title") or "Untitled")
        channel_text = str(video.get("channel_title") or video.get("author") or "")
        thumbnail = escape(_thumbnail_for(video), quote=True)
        image = f'<img src="{thumbnail}" alt="" loading="lazy" onerror="this.remove()">' if thumbnail else ""
        duration = escape(_duration_text(video.get("duration_seconds")))
        duration_html = f'<span class="duration">{duration}</span>' if duration else ""
        if index == position:
            state = ' class="watch-list-item is-current" aria-current="true"'
            marker = f'{_icon("play")}<span class="sr-only">Playing</span>'
        else:
            state = ' class="watch-list-item" data-list-next' if index == position + 1 else ' class="watch-list-item"'
            marker = str(index + 1)
        rows.append(
            f'<li{state}><a href="{_list_watch_href(video_id, key, shuffle)}">'
            f'<span class="watch-list-index" aria-hidden="{"false" if index == position else "true"}">{marker}</span>'
            f'<span class="watch-list-thumb">{image}{duration_html}</span>'
            f'<span class="watch-list-copy"><span class="watch-list-title">{escape(title_text)}</span>'
            f'<span class="watch-list-channel">{escape(channel_text)}</span></span></a></li>'
        )
    mode = "Shuffled · " if shuffle else ""
    return (
        f'<section class="watch-list" data-watch-list data-list-key="{escape(key, quote=True)}" aria-labelledby="watch-list-title">'
        f'<div class="watch-list-head"><h2 id="watch-list-title"><a href="{escape(watch_list["href"], quote=True)}">{escape(watch_list["title"])}</a></h2>'
        f'<p class="watch-list-meta" data-list-position>{mode}{position + 1} of {total}</p></div>'
        f'<ol class="watch-list-items" aria-label="{escape(watch_list["title"], quote=True)}, in play order">{"".join(rows)}</ol>'
        f'</section>'
    )


def _liked_section_html(user_key: str, settings: Any, limit: int = 120, sort: str = "recent") -> str:
    sort_key = library_tools.clean_sort(sort, library_tools.LIKED_SORTS)
    liked = db.list_liked_videos(limit, user_key, oldest=sort_key == "oldest") if sort_key == "oldest" else db.list_liked_videos(limit, user_key)
    if not liked:
        return _page_head("Liked videos") + _empty_state(
            "like", "No liked videos",
            "Like a video on its watch page and it lands here.",
            '<a class="btn btn-sm" href="/">Browse home</a>',
        )
    rows = []
    for video in liked:
        when = _relative_time(video.get("latest_event_at"))
        video_id = escape(str(video.get("youtube_video_id") or ""), quote=True)
        rows.append(_media_row(
            video, settings, "Liked",
            meta=[f"liked {when}"] if when else [],
            menu_rows=_menu_row("trash", "Remove from Liked videos", attrs=f' data-remove-liked="{video_id}"', danger=True),
            variant="library",
            filter_text=f"{video.get('title') or ''} {video.get('channel_title') or video.get('author') or ''}",
            shorts_route=True,
        ))
    tools = _sort_menu(
        library_tools.LIKED_SORTS, sort_key, lambda key: "/liked" if key == "recent" else f"/liked?sort={key}",
    ) + _select_button("liked videos")
    layout = _detail_layout(
        "Liked videos",
        [_liked_count_text(len(liked), limit), _list_runtime(liked)],
        escape(_thumbnail_for(liked[0])), len(liked),
        _play_all_buttons(liked, "liked"),
        f'<div class="library-list">{"".join(rows)}</div>',
        icon="like", filter_label="Filter liked videos", noun="liked video", tools=tools,
    )
    return f'<div class="select-host"{_select_scope_attrs("liked", noun="Liked videos")}>{layout}</div>'


def _quota_strip(used: int, quota: int, members_may_download: bool = True) -> str:
    """A member's own kept downloads against their quota. Nothing about the server's disk
    or anyone else's downloads."""
    if not members_may_download:
        return ('<section class="storage-strip" aria-label="Storage" data-storage-quota>'
                '<p>Downloading is turned off for members on this server. What you kept before still plays.</p></section>')
    if not quota:
        return (f'<section class="storage-strip" aria-label="Storage" data-storage-quota>'
                f'<p>{escape(_bytes_text(used) or "0 B")} kept on this server.</p></section>')
    share = min(100.0, used / quota * 100) if quota else 0.0
    left = max(0, quota - used)
    state = " is-full" if used >= quota else (" is-near" if share >= 85 else "")
    width = f"max(2px, {share:.2f}%)" if used else "0"
    return (
        f'<section class="storage-strip{state}" aria-label="Storage" data-storage-quota>'
        f'<div class="storage-bar" role="meter" aria-label="Your download storage" aria-valuemin="0" aria-valuemax="{quota}" '
        f'aria-valuenow="{min(used, quota)}" aria-valuetext="{escape(_bytes_text(used) or "0 B")} of {escape(_bytes_text(quota))}">'
        f'<span class="is-downloads" style="width:{width}"></span></div>'
        f'<p>{escape(_bytes_text(used) or "0 B")} of your {escape(_bytes_text(quota))} &middot; '
        f'{escape(_bytes_text(left) or "0 B") + " left" if left else "Full: delete something to keep more"}</p></section>'
    )


def _storage_strip() -> str:
    """Downloads against the disk they live on, from the filesystem and nothing else."""
    try:
        usage = downloads.disk_usage()
    except OSError:
        return ""
    used = int(usage.get("bytes") or 0)
    free, total = usage.get("free_bytes"), usage.get("total_bytes")
    legend = [f"{_bytes_text(used) or '0 B'} in downloads"]
    if free is not None:
        legend.append(f"{_bytes_text(free)} free")
    bar = ""
    if total:
        share = min(100.0, used / total * 100)
        other = max(0.0, min(100.0 - share, (total - used - int(free or 0)) / total * 100))
        # A sliver stays visible however small the share: "some" and "none" are different facts.
        width = f"max(2px, {share:.2f}%)" if used else "0"
        bar = (
            f'<div class="storage-bar" aria-hidden="true"><span class="is-downloads" style="width:{width}"></span>'
            f'<span class="is-other" style="width:{other:.2f}%"></span></div>'
        )
    return f'<section class="storage-strip" aria-label="Storage">{bar}<p>{" &middot; ".join(escape(part) for part in legend)}</p></section>'


def _downloads_section_html(rows: list[dict[str, Any]], user_key: str, settings: Any, viewer: dict[str, Any] | None = None) -> str:
    active = sum(
        1 for job in downloads.manager.list_jobs()
        if getattr(job, "user_key", None) == user_key and job.status in {"queued", "downloading", "merging"}
    )
    if rows:
        library = (
            f'{_library_filter("Filter downloads")}'
            f'<div class="library-list downloads-list" data-downloads-grid>{"".join(_download_card(row, settings) for row in rows)}</div>'
            f'{_filter_empty("download")}'
        )
    else:
        library = f'<div data-downloads-grid>{_empty_state(
            "download", "Nothing kept on the server",
            "On a watch page, open More actions, choose Download, then Keep on server. The video appears here and plays without touching YouTube.",
        )}</div>'
    job_row = f'''
      <template data-download-job-template>
        <article class="media-row library-row job-row" data-job-row>
          <span class="thumb"><span class="thumb-fallback" aria-hidden="true">{_icon("download")}</span><img alt="" loading="lazy" onerror="this.remove()" data-job-thumb></span>
          <div class="media-row-copy">
            <h2 data-job-title></h2>
            <p class="row-byline" data-job-stats></p>
            <div class="download-bar" data-job-bar><span></span></div>
          </div>
          <div class="row-actions is-pinned"><button class="btn btn-sm" type="button" data-job-cancel>Cancel</button></div>
        </article>
      </template>
      <template data-download-failed-template>
        <article class="failed-job" data-job-row>
          <div class="failed-job-copy">
            <h3 data-job-title></h3>
            <p data-job-error></p>
          </div>
          <div class="failed-job-actions">
            <button class="btn btn-sm" type="button" data-job-retry>Retry</button>
            <button class="btn btn-sm btn-ghost" type="button" data-job-dismiss>Remove</button>
          </div>
        </article>
      </template>
    '''
    # The disk view is the server's (every account's files, the free space): an admin's
    # and the single user's. A member sees their own use, against their quota if any.
    if viewer is not None and policy.is_member(viewer):
        try:
            used = sum(int(row.get("size_bytes") or 0) for row in rows) if not instance.download_quota_bytes() \
                else downloads.account_usage_bytes(policy.account_key(viewer))
        except Exception:  # noqa: BLE001 - the list still renders
            used = sum(int(row.get("size_bytes") or 0) for row in rows)
        strip = _quota_strip(used, instance.download_quota_bytes(), instance.members_may_download()) \
            if rows or instance.download_quota_bytes() or not instance.members_may_download() else ""
    else:
        strip = _storage_strip() if rows else ""
    placeholders = _skeleton_rows(active)
    return f'''
      <div data-downloads-page>
        {strip}
        <section class="downloads-section" data-downloads-active{"" if active else " hidden"}>
          <h2 class="list-label">Downloading</h2>
          <div class="library-list downloads-list" data-downloads-active-list>{placeholders}</div>
        </section>
        <section class="downloads-section" data-downloads-failed hidden>
          <h2 class="list-label">Failed</h2>
          <div class="failed-list" data-downloads-failed-list></div>
        </section>
        {library}
        {job_row}
      </div>
    '''


# ---- Library tools: Sort, Select, History pause and clear, playlist order and privacy ----
# The behaviour is in assets/js/library-tools.js, the data in library_tools.py.


def _sort_menu(options: tuple[tuple[str, str], ...], selected: str, href_for: Any, row_attrs: Any = None) -> str:
    """The list's Sort button: a menu of links with the current order checked.

    options are (key, label) with the default first; href_for maps a key to a raw URL and
    row_attrs to extra trusted attributes for that row (the owner's playlist sort, which
    the script also saves).
    """
    current = next((label for key, label in options if key == selected), options[0][1])
    rows = "".join(
        _menu_row(
            "check", label, href=escape(href_for(key), quote=True),
            trailing="✓" if key == selected else "",
            attrs=(' aria-current="true"' if key == selected else "") + (row_attrs(key) if row_attrs else ""),
        )
        for key, label in options
    )
    return (
        f'<details class="filter-menu yt-menu sort-menu">'
        f'<summary class="btn btn-ghost list-tool" aria-label="Sort by: {escape(current, quote=True)}" data-tip="Sort by">'
        f'{_icon("sort")}<span>Sort</span></summary>'
        f'<div class="menu-panel" role="menu" aria-label="Sort by">{rows}</div></details>'
    )


def _select_button(noun: str) -> str:
    """Enters select mode for the list on this page (library-tools.js)."""
    return (
        f'<button class="btn btn-ghost list-tool" type="button" data-select-toggle aria-pressed="false" '
        f'aria-label="Select {escape(noun, quote=True)}">{_icon("select")}<span>Select</span></button>'
    )


def _select_scope_attrs(kind: str, playlist_id: str = "", can_remove: bool = True, noun: str = "video") -> str:
    """Attributes for the element that holds a selectable list. Values are escaped here."""
    attrs = f' data-select-scope data-select-kind="{escape(kind, quote=True)}" data-select-noun="{escape(noun, quote=True)}"'
    if playlist_id:
        attrs += f' data-playlist-id="{escape(playlist_id, quote=True)}"'
    if not can_remove:
        attrs += " data-select-readonly"
    return attrs


def _history_controls(paused: bool) -> str:
    """YouTube's History side panel: clear everything, pause or turn history back on."""
    pause_label = "Turn on watch history" if paused else "Pause watch history"
    return (
        '<aside class="history-side" aria-label="Watch history controls">'
        f'<button class="history-control" type="button" data-history-clear>{_icon("trash")}<span>Clear all watch history</span></button>'
        f'<button class="history-control" type="button" data-history-pause="{"off" if paused else "on"}">'
        f'{_icon("play" if paused else "pause")}<span>{pause_label}</span></button>'
        '</aside>'
    )


def _history_paused_banner() -> str:
    return (
        '<section class="history-paused" aria-labelledby="history-paused-title">'
        f'<span class="history-paused-icon" aria-hidden="true">{_icon("pause")}</span>'
        '<div class="history-paused-copy"><h2 id="history-paused-title">Watch history is paused</h2>'
        "<p>Videos you watch aren't added here, don't keep a resume point and don't shape your recommendations.</p></div>"
        '<button class="btn btn-sm" type="button" data-history-pause="off">Turn on</button>'
        '</section>'
    )


def _playlist_move_rows(video_id: str, title_text: str) -> str:
    """Move to top, Move to bottom, Move up, Move down: reordering without dragging."""
    attrs = f' data-playlist-move="{{move}}" data-video-id="{escape(video_id, quote=True)}"'
    return "".join(
        _menu_row(icon, label, attrs=attrs.replace("{move}", move))
        for icon, label, move in (
            ("move-top", "Move to top", "top"),
            ("move-bottom", "Move to bottom", "bottom"),
            ("arrow-up", "Move up", "up"),
            ("arrow-down", "Move down", "down"),
        )
    ) + '<div class="menu-separator" role="separator"></div>'


def _drag_handle(title_text: str) -> str:
    """The row's drag handle. A button, so arrow keys move the row from the keyboard."""
    label = escape(f"Reorder {title_text}", quote=True)
    return (
        f'<button class="row-drag" type="button" data-drag-handle aria-label="{label}" '
        f'aria-describedby="playlist-reorder-help" title="Drag to reorder">{_icon("grip")}</button>'
    )


def _visibility_menu(playlist_id: str, visibility: str) -> str:
    """The owner's Private / Unlisted / Public switch, YouTube's three, plus Copy link."""
    current = library_tools.clean_visibility(visibility)
    playlist_attr = escape(playlist_id, quote=True)
    rows = "".join(
        _menu_row(
            library_tools.VISIBILITY_ICONS[key], label, sub=sub,
            trailing="✓" if key == current else "",
            attrs=f' data-playlist-visibility="{key}" data-playlist-id="{playlist_attr}"'
            + (' aria-current="true"' if key == current else ""),
        )
        for key, label, sub in library_tools.VISIBILITIES
    )
    copy = _menu_row("share", "Copy link", attrs=f' data-copy-url="/playlist/{playlist_attr}"')
    return (
        '<details class="yt-menu visibility-menu">'
        f'<summary class="visibility-pill" aria-label="Visibility: {escape(library_tools.visibility_label(current), quote=True)}" data-visibility-summary>'
        f'{_icon(library_tools.VISIBILITY_ICONS[current])}<span>{escape(library_tools.visibility_label(current))}</span>{_icon("chevron-down")}</summary>'
        f'<div class="menu-panel" role="menu" aria-label="Playlist visibility">{rows}'
        f'<div class="menu-separator" role="separator"></div>{copy}</div></details>'
    )


def _visibility_note(visibility: str, owner_name: str) -> str:
    """What a viewer who is not the owner sees in place of the switch."""
    key = library_tools.clean_visibility(visibility)
    owner = f"by {owner_name}" if owner_name else "Shared from another profile"
    return (
        f'<span class="visibility-pill is-static">{_icon(library_tools.VISIBILITY_ICONS[key])}'
        f'<span>{escape(library_tools.visibility_label(key))}</span></span>'
        f'<span class="playlist-owner">{escape(owner)}</span>'
    )


def _playlist_import_dialog() -> str:
    """Import a YouTube playlist by link. Opened from Playlists and the New playlist dialog."""
    options = "".join(
        f'<option value="{key}"{" selected" if key == "private" else ""}>{escape(label)}</option>'
        for key, label, _ in library_tools.VISIBILITIES
    )
    return f'''
      <dialog class="feed-dialog playlist-import-dialog" id="playlist-import-dialog" aria-labelledby="playlist-import-title">
        <form data-playlist-import-form>
          <div class="dialog-head">
            <div>
              <h2 id="playlist-import-title">Import a YouTube playlist</h2>
              <p>Paste its link. OpenFeed reads it through your Invidious and keeps a copy here, in YouTube's order.</p>
            </div>
            <button class="icon-button" type="button" data-close-dialog aria-label="Close">{_icon("x")}</button>
          </div>
          <div class="dialog-body">
            <label class="text-field">Playlist link<input name="url" type="text" inputmode="url" required maxlength="400" autocomplete="off" spellcheck="false" placeholder="https://www.youtube.com/playlist?list=…"></label>
            <label class="text-field">Name<input name="name" type="text" maxlength="120" autocomplete="off" placeholder="Same as on YouTube"></label>
            <label class="text-field">Visibility<select class="field-input" name="visibility">{options}</select></label>
            <div class="import-progress" data-pl-import-progress hidden>
              <div class="download-bar" data-pl-import-bar><span></span></div>
              <p class="import-progress-text" data-pl-import-text role="status" aria-live="polite"></p>
            </div>
          </div>
          <div class="dialog-actions">
            <p class="status-line" data-pl-import-status role="alert"></p>
            <button class="btn btn-ghost" type="button" data-close-dialog>Cancel</button>
            <a class="btn btn-primary" href="#" data-pl-import-open hidden>Open playlist</a>
            <button class="btn btn-primary" type="submit" data-pl-import-submit>Import</button>
          </div>
        </form>
      </dialog>
    '''


def _shared_playlists_section(user_key: str) -> str:
    """Public playlists from the other profiles and accounts on this install."""
    if _kids_profile(user_key):
        return ""
    try:
        shared = library_tools.list_shared_playlists(user_key)
    except Exception as exc:  # noqa: BLE001 - the page works without the shared shelf
        logger.info("shared playlists unavailable: %s", exc)
        return ""
    if not shared:
        return ""
    cards = "".join(_playlist_card(playlist, owner_view=False) for playlist in shared)
    return (
        '<section class="section-block shared-playlists" aria-labelledby="shared-playlists-title">'
        '<div class="section-heading"><h2 id="shared-playlists-title">Public on this OpenFeed</h2></div>'
        f'<div class="playlist-grid">{cards}</div></section>'
    )


def _library_page_shell(request: Request, title: str, body: str, nav_key: str, meta: str = "", actions: str = ""):
    return _app_shell(title, nav_key, f"{_page_head(title, meta, actions)}{body}", request=request)


def _kids_profile(user_key: str) -> bool:
    try:
        return bool(db.get_feed_preferences(user_key).get("kids_profile"))
    except Exception:  # noqa: BLE001 - no database, no profiles
        return False


def _shorts_hidden(user_key: str) -> bool:
    """Show Shorts is off (exclude_shorts), which a kids profile always has."""
    try:
        return bool(db.get_feed_preferences(user_key).get("exclude_shorts"))
    except Exception:  # noqa: BLE001 - no database: nothing to hide by
        return False


def _shorts_off_state(settings_href: str) -> str:
    """What stands in for Shorts while Show Shorts is off. settings_href is trusted."""
    return _empty_state(
        "shorts", "Shorts are off",
        "Show Shorts is switched off in Settings, so Shorts are hidden everywhere. Turn it on to watch them here.",
        f'<a class="btn" href="{settings_href}">Open Content settings</a>',
    )


def _age_restricted(video: dict[str, Any]) -> bool:
    """YouTube's own flag, as Invidious reports it. Only an explicit false counts."""
    metadata = video.get("metadata") if isinstance(video.get("metadata"), dict) else {}
    return metadata.get("isFamilyFriendly") is False


def _kids_refusal(request: Request, title: str, body: str, active: str = "", status_code: int = 403) -> HTMLResponse:
    action = '<a class="btn" href="/">Back to Home</a>'
    content = f'<h1 class="sr-only">{escape(title)}</h1>{_empty_state("user", title, body, action)}'
    return HTMLResponse(_app_shell(title, active, content, request=request), status_code=status_code)


def _page_not_found(request: Request | None, title: str, body: str, href: str, label: str, active: str, icon: str = "search") -> HTMLResponse:
    """A 404 for a page route: the app shell for a browser, the usual JSON for anything else.

    An API client asking for one of these URLs keeps {"detail": title}. href is escaped here.
    """
    if request is None or "text/html" not in request.headers.get("accept", "").lower():
        raise HTTPException(status_code=404, detail=title)
    action = f'<a class="btn" href="{escape(href, quote=True)}">{escape(label)}</a>'
    content = f'<h1 class="sr-only">{escape(title)}</h1>{_empty_state(icon, title, body, action)}'
    return HTMLResponse(_app_shell(title, active, content, request=request), status_code=404)


@app.get("/history", response_class=HTMLResponse, tags=["pages"], summary="Watch history")
def history_page(request: Request, limit: int = 120, sort: str = ""):
    auth_redirect = _login_required_redirect(request)
    if auth_redirect:
        return auth_redirect
    user_key = _user_key(request)
    limit = max(1, min(limit, 400))
    sort_key = library_tools.clean_sort(sort, library_tools.HISTORY_SORTS)
    # One past the page, which is the cheapest honest answer to "is there more?".
    history = db.list_history_videos(limit + 1, user_key, oldest=sort_key == "oldest")
    more = len(history) > limit
    shown = history[:limit]
    meta = f"{'Oldest' if sort_key == 'oldest' else 'Latest'} {limit}" if more else (_count_text(len(shown)) if shown else "")
    next_limit = min(400, limit * 2) if more and limit < 400 else 0
    paused = library_tools.history_paused(user_key)
    tools = ""
    if shown:
        tools = _sort_menu(
            library_tools.HISTORY_SORTS, sort_key,
            lambda key: "/history" if key == "newest" else f"/history?sort={key}",
        ) + _select_button("videos in history")
    body = (
        '<div class="history-layout">'
        f'<div class="history-main"{_select_scope_attrs("history", noun="history")}>'
        f'{_history_paused_banner() if paused else ""}{_history_section_html(shown, get_settings(), next_limit, sort_key, tools)}</div>'
        f'{_history_controls(paused)}</div>'
    )
    not_interested = f'<a class="btn btn-ghost btn-sm" href="/not-interested">{_icon("block")}<span>Not interested</span></a>'
    return _library_page_shell(request, "History", body, "history", meta, not_interested)


@app.get("/playlists", response_class=HTMLResponse, tags=["pages"], summary="Playlists")
def playlists_page(request: Request, limit: int = 120):
    auth_redirect = _login_required_redirect(request)
    if auth_redirect:
        return auth_redirect
    user_key = _user_key(request)
    custom = db.list_playlists(user_key)
    import_button = (
        f'<button class="btn" type="button" data-open-playlist-import>{_icon("import")}<span>Import from YouTube</span></button>'
    )
    return _library_page_shell(
        request, "Playlists",
        _playlists_section_html(custom, user_key, max(1, min(limit, 400))) + _shared_playlists_section(user_key), "playlists",
        _count_text(len(custom), "playlist") if custom else "",
        actions=import_button,
    )


@app.get("/liked", response_class=HTMLResponse, tags=["pages"], summary="Liked videos")
def liked_page(request: Request, limit: int = 120, sort: str = ""):
    auth_redirect = _login_required_redirect(request)
    if auth_redirect:
        return auth_redirect
    user_key = _user_key(request)
    content = _liked_section_html(user_key, get_settings(), max(1, min(limit, 400)), sort)
    return _app_shell("Liked videos", "liked", content, request=request)


@app.get("/downloads", response_class=HTMLResponse, tags=["pages"], summary="Downloaded videos")
def downloads_page(request: Request):
    auth_redirect = _login_required_redirect(request)
    if auth_redirect:
        return auth_redirect
    user_key = _user_key(request)
    rows = db.list_downloads(user_key, "", 500)
    return _library_page_shell(
        request, "Downloads", _downloads_section_html(rows, user_key, get_settings(), viewer=_current_user(request)), "downloads",
        _count_text(len(rows), "download") if rows else "",
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


def _viewable_playlist(playlist_id: str, user_key: str) -> dict[str, Any] | None:
    """This profile's playlist, or one another profile shared as unlisted or public.

    The owner's own lookup comes first and is the one the rest of the app uses; the
    shared lookup is a fallback, so a database hiccup there reads as "not found".
    """
    playlist = db.get_playlist(playlist_id, user_key)
    if playlist:
        return {**playlist, "is_owner": True}
    try:
        return library_tools.get_viewable_playlist(playlist_id, user_key)
    except Exception as exc:  # noqa: BLE001
        logger.info("shared playlist lookup failed: %s", exc)
        return None


@app.get("/playlist/{playlist_id}", response_class=HTMLResponse)
def playlist_page(request: Request, playlist_id: str, sort: str = ""):
    auth_redirect = _login_required_redirect(request)
    if auth_redirect:
        return auth_redirect
    settings = get_settings()
    user_key = _user_key(request)
    if not _valid_uuid(playlist_id):
        return _page_not_found(request, "Playlist not found", "It may have been deleted, or the link is wrong.", "/playlists", "All playlists", "playlists", "playlist")
    playlist = _viewable_playlist(playlist_id, user_key)
    if not playlist:
        return _page_not_found(request, "Playlist not found", "It may have been deleted, or the link is wrong.", "/playlists", "All playlists", "playlists", "playlist")
    owner = bool(playlist.get("is_owner"))
    owner_key = str(playlist.get("user_key") or user_key)
    # The owner's chosen sort is the playlist's order everywhere (Play all included);
    # ?sort= views it another way without changing it.
    stored_sort = library_tools.clean_sort(playlist.get("sort_key"), library_tools.PLAYLIST_SORTS)
    sort_key = library_tools.clean_sort(sort, library_tools.PLAYLIST_SORTS) if sort else stored_sort
    videos = library_tools.sort_playlist_videos(db.list_playlist_videos(playlist_id, 300, owner_key), sort_key)
    reorderable = owner and sort_key == "manual" and len(videos) > 1
    name_text = str(playlist.get("name") or "Untitled playlist")
    playlist_id_attr = escape(str(playlist.get("id") or ""), quote=True)
    description_text = str(playlist.get("description") or "")
    visibility = library_tools.clean_visibility(playlist.get("visibility"))
    count = int(playlist.get("video_count") or len(videos))
    updated = _relative_time(playlist.get("latest_video_at") or playlist.get("updated_at"))
    meta = [
        _count_text(count),
        _runtime_text(playlist.get("runtime_seconds"), playlist.get("runtime_known"), count),
        f"updated {updated}" if updated else "",
    ]
    cover = escape(_thumbnail_for(videos[0])) if videos else ""
    # Opens the watch page in list mode (?list=), which queues the rest of the playlist.
    play_all = _play_all_buttons(videos, str(playlist.get("id") or ""))
    rows = []
    for index, video in enumerate(videos, start=1):
        video_id_raw = str(video.get("youtube_video_id") or "")
        video_id = escape(video_id_raw, quote=True)
        title_text = str(video.get("title") or "Untitled")
        hooks = f'data-remove-playlist-item="{video_id}" data-playlist-id="{playlist_id_attr}"'
        added = _relative_time(video.get("added_at"))
        menu_rows = ""
        if reorderable:
            menu_rows += _playlist_move_rows(video_id_raw, title_text)
        if owner:
            menu_rows += _menu_row("x", "Remove from playlist", attrs=f" {hooks}", danger=True)
        rows.append(
            _media_row(
                video,
                settings,
                index=index,
                playlist_row_id=video_id_raw or None,
                trailing=_row_remove(hooks, f"Remove from playlist: {title_text}") if owner else "",
                meta=[f"added {added}"] if added else [],
                menu_rows=menu_rows,
                variant="library",
                filter_text=f"{title_text} {video.get('channel_title') or video.get('author') or ''}",
                lead=_drag_handle(title_text) if reorderable else "",
            )
        )
    if owner:
        actions = (
            f'{play_all}'
            f'<button class="btn" type="button" data-edit-playlist="{playlist_id_attr}" data-playlist-name="{escape(name_text, quote=True)}" data-playlist-description="{escape(description_text, quote=True)}" data-playlist-visibility="{visibility}" aria-label="Edit playlist" data-playlist-rename>{_icon("pencil")}<span>Edit</span></button>'
            f'<button class="btn btn-danger btn-icon" type="button" data-delete-playlist="{playlist_id_attr}" data-playlist-name="{escape(name_text, quote=True)}" data-redirect="/playlists" aria-label="Delete playlist" title="Delete playlist">{_icon("trash")}</button>'
        )
        byline = _visibility_menu(str(playlist.get("id") or ""), visibility)
    else:
        actions = play_all
        byline = _visibility_note(visibility, str(playlist.get("owner_name") or ""))
    base_href = f"/playlist/{playlist.get('id') or ''}"

    def sort_href(key: str) -> str:
        return base_href if key == stored_sort else f"{base_href}?sort={key}"

    tools = ""
    if len(videos) > 1:
        tools += _sort_menu(
            library_tools.PLAYLIST_SORTS, sort_key, sort_href,
            (lambda key: f' data-playlist-sort="{key}" data-playlist-id="{playlist_id_attr}"') if owner else None,
        )
    if videos:
        tools += _select_button("videos in this playlist")
    reorder_help = (
        '<p class="sr-only" id="playlist-reorder-help">Drag, or press the up and down arrow keys, to move this video. '
        'Each video\'s menu also has Move to top and Move to bottom.</p>'
        if reorderable else ""
    )
    items_html = (
        f'{reorder_help}<div class="library-list"{" data-reorder-list" if reorderable else ""} data-playlist-id="{playlist_id_attr}">{"".join(rows)}</div>' if rows else _empty_state(
            "playlist", "This playlist is empty",
            "Choose Save to playlist from any video's menu and it lands here.",
            '<a class="btn btn-sm" href="/channels">Browse subscriptions</a>',
        )
    )
    content = _detail_layout(
        name_text, meta, cover, count, actions, items_html,
        description=description_text,
        filter_label="Filter this playlist" if rows else "", noun="video",
        tools=tools, byline=byline,
    )
    scope = _select_scope_attrs("playlist", str(playlist.get("id") or ""), can_remove=owner, noun=name_text)
    return _app_shell(name_text, "playlists", f'<div class="select-host"{scope}>{content}</div>', request=request)


@app.get("/channel/{youtube_channel_id}/posts", response_class=HTMLResponse, tags=["pages"], summary="Channel community posts")
def channel_posts_page(request: Request, youtube_channel_id: str, continuation: str = ""):
    """The Posts tab. Available whether or not posts are enabled in the feeds."""
    auth_redirect = _login_required_redirect(request)
    if auth_redirect:
        return auth_redirect
    from . import posts as posts_module

    user_key = _user_key(request)
    channel = db.get_channel_by_youtube_id(youtube_channel_id, user_key)
    if not channel:
        return _page_not_found(request, "Channel not found", "OpenFeed has no channel with this ID. The link may be wrong, or the channel was never added here.", "/channels", "All channels", "channels", "channels")
    try:
        page = posts_module.channel_posts(youtube_channel_id, continuation)
        error = ""
    except posts_module.PostsError as exc:
        page, error = {"posts": [], "continuation": ""}, _safe_exc_name(exc)

    items = page.get("posts") or []
    votes = {post["post_id"]: db.get_poll_vote(post["post_id"], user_key) for post in items}
    cards = "".join(_post_card(post, votes.get(post["post_id"])) for post in items)
    channel_key = escape(youtube_channel_id, quote=True)
    if cards:
        listing = f'<div class="post-list">{cards}</div>'
    elif error:
        listing = _empty_state(
            "status", "Posts could not be loaded", f"The request to this channel failed: {error}.",
            f'<a class="btn" href="/channel/{channel_key}/posts">Try again</a>',
        )
    else:
        listing = _empty_state("channels", "No posts", "This channel has no community posts, or has them turned off.")
    more = ""
    if page.get("continuation"):
        more = (
            f'<div class="post-more"><a class="btn" href="/channel/{channel_key}'
            f'/posts?continuation={urllib.parse.quote(page["continuation"])}">Load older posts</a></div>'
        )
    title_text = str(channel.get("title") or "Channel")
    content = f'''
      <div class="ch-wrap">
      {_channel_head(channel, youtube_channel_id, "posts", user_key)}
      {listing}
      {more}
      </div>
    '''
    return _app_shell(f"{title_text} posts", "channels", content, main_class="channel-main", request=request)


@app.get("/channel/{youtube_channel_id}/posts/{post_id}", response_class=HTMLResponse, tags=["pages"], summary="A single post")
def post_detail_page(request: Request, youtube_channel_id: str, post_id: str):
    auth_redirect = _login_required_redirect(request)
    if auth_redirect:
        return auth_redirect
    from . import posts as posts_module

    user_key = _user_key(request)
    post = posts_module.single_post(post_id, youtube_channel_id)
    if not post:
        return _page_not_found(request, "Post not found", "YouTube did not return this post. It may have been removed.", f"/channel/{urllib.parse.quote(youtube_channel_id, safe='')}/posts", "All posts", "channels")
    replies = posts_module.post_comments(post_id, youtube_channel_id)
    reply_html = "".join(
        f'<article class="comment">{_avatar_html(str(item.get("author") or ""), _comment_avatar(item.get("authorThumbnails")))}'
        f'<div><div class="comment-head"><span class="comment-author">{escape(str(item.get("author") or ""))}</span>'
        f'<span class="comment-time">{escape(str(item.get("publishedText") or ""))}</span></div>'
        f'<p class="comment-body">{escape(str(item.get("content") or ""))}</p></div></article>'
        for item in (replies.get("comments") or [])[:50]
        if isinstance(item, dict)
    )
    content = f'''
      <div class="post-detail">
        <a class="btn btn-ghost btn-sm post-back" href="/channel/{escape(youtube_channel_id, quote=True)}/posts">{_icon("chevron-left")}<span>All posts</span></a>
        <div class="post-list">{_post_card(post, db.get_poll_vote(post_id, user_key))}</div>
        {f'<section class="comments"><div class="comments-head"><h2>Replies</h2></div>{reply_html}</section>' if reply_html else ""}
      </div>
    '''
    return _app_shell("Post", "channels", content, main_class="channel-main", request=request)


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
            if str(url or "").lower().startswith(("http://", "https://"))
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
        info_rows.append(f'<li>{_icon("user")}<span>{_count_label(escape(subscriber_count), "subscriber")}</span></li>')
    info_rows.append(f'<li>{_icon("play")}<span>{_count_label(video_count, "video")} in your library</span></li>')
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


def _channel_head(channel: dict[str, Any], youtube_channel_id: str, active_tab: str, user_key: str, query: str = "", video_count: int = 0) -> str:
    """Banner, identity, actions and tabs: everything above a channel's content.

    The tab content scrolls into the first screen because this stays short: at 1440px
    a full row of videos has to sit above the fold, so the banner is a strip and the
    bio stops at two lines, with the whole text in the "...more" dialog.
    """
    title_text = str(channel.get("title") or "Unknown channel")
    channel_key = escape(youtube_channel_id, quote=True)
    count = int(channel.get("video_count") or video_count or 0)
    latest = _relative_time(channel.get("latest_video_at"))
    subscription_link = _safe_external_href(channel.get("subscription_html_url"))  # raw; escaped per use
    feed_url_raw = str(channel.get("feed_url") or _youtube_feed_url(youtube_channel_id))
    subscribed = bool(channel.get("subscription_id"))
    notify_level = db.get_channel_notification(youtube_channel_id, user_key) if subscribed else "all"
    subscribe_button = _subscribe_control(youtube_channel_id, feed_url_raw, title_text, subscribed, notify_level)
    banner_url = privacy.same_origin_image(channel.get("banner_url"))
    # No banner, no strip: an empty band would only push the videos down.
    banner_html = (
        f'<section class="channel-banner has-image" aria-hidden="true">'
        f'<img src="{escape(banner_url, quote=True)}" alt="" decoding="async" '
        f'onerror="this.closest(\'.channel-banner\').classList.remove(\'has-image\'); this.remove()"></section>'
    ) if banner_url else ""

    subscriber_count = _compact_count(channel.get("subscriber_count"))
    handle = str(channel.get("handle") or "").strip()
    verified = _icon("verified").replace('class="icon"', 'class="icon verified"') if channel.get("verified") else ""
    identity_parts = []
    if handle:
        identity_parts.append(f'<span class="ch-handle">@{escape(handle.lstrip("@"))}</span>')
    if subscriber_count:
        identity_parts.append(_count_label(escape(subscriber_count), "subscriber"))
    identity_parts.append(f"{_count_label(count, 'video')} here")
    if latest:
        identity_parts.append(f"latest {escape(latest)}")
    identity = ' <span class="ch-dot">&middot;</span> '.join(identity_parts)

    description = str(channel.get("description") or "").strip()
    # Two clamped lines and a "...more", with the whole text behind a dialog. Expanding in
    # place is what made the header jump, and it is the first thing on the page.
    summary = " ".join(description.split())
    description_html = (
        f'<div class="ch-about">'
        f'<p class="ch-about-short">{_rich_text(summary[:320])}</p>'
        f'<button class="ch-about-more" type="button" data-ch-about-open>&hellip;more</button>'
        f'</div>'
    ) if description else ""

    # YouTube shows a dedicated links row. Invidious exposes no link fields, so the only
    # honest source is the URLs a creator wrote into their own description.
    found_links = _description_links(description)
    links_html = ""
    if found_links:
        first_label, first_url = found_links[0]
        extra = (
            f'<button class="ch-links-more" type="button" data-ch-about-open>and {len(found_links) - 1} more</button>'
            if len(found_links) > 1 else ""
        )
        links_html = (
            f'<p class="ch-links">{_icon("external")}'
            f'<a href="{escape(first_url, quote=True)}" target="_blank" rel="noopener noreferrer nofollow">'
            f'{escape(_link_label(first_url))}</a>{extra}</p>'
        )

    about_dialog = _channel_about_dialog(
        title_text, description, found_links, youtube_channel_id, handle,
        subscriber_count, count, latest, subscription_link,
    )

    available = {str(tab).lower() for tab in (channel.get("available_tabs") or [])}
    tabs = [("videos", "Videos", f"/channel/{channel_key}")]
    if not available or "shorts" in available:
        tabs.append(("shorts", "Shorts", f"/channel/{channel_key}?filter=shorts"))
    if channel_tools.channel_has_live_tab(available):
        tabs.append(("live", "Live", f"/channel/{channel_key}?filter=live"))
    if not available or "playlists" in available:
        tabs.append(("playlists", "Playlists", f"/channel/{channel_key}?filter=playlists"))
    tabs.append(("posts", "Posts", f"/channel/{channel_key}/posts"))
    tab_html = "".join(
        f'<a class="ch-tab{" active" if key == active_tab else ""}" href="{href}"'
        f'{" aria-current=\"page\"" if key == active_tab else ""}>{label}</a>'
        for key, label, href in tabs
    )

    overflow_rows = []
    subscription_id_raw = str(channel.get("subscription_id") or "")
    if subscription_id_raw:
        overflow_rows.append(_menu_row(
            "collections", "Add to collection",
            attrs=f' data-channel-collections="{escape(subscription_id_raw, quote=True)}" data-channel-title="{escape(title_text, quote=True)}"'))
    youtube_link = subscription_link or f"https://www.youtube.com/channel/{urllib.parse.quote(youtube_channel_id, safe='')}"
    overflow_rows.append(_menu_row("external", "Open on YouTube", href=escape(youtube_link, quote=True)))
    overflow = f'''
      <details class="ch-menu yt-menu">
        <summary class="pill-action pill-icon" aria-label="More channel actions">{_icon("more")}</summary>
        <div class="menu-panel" role="menu" aria-label="More channel actions">{"".join(overflow_rows)}</div>
      </details>
    '''

    return f'''
      {banner_html}
      <section class="ch-identity">
        {_avatar_html(title_text, channel.get("avatar_url"), css_class="avatar ch-avatar")}
        <div class="ch-identity-copy">
          <h1><span>{escape(title_text)}</span>{verified}</h1>
          <p class="ch-meta">{identity}</p>
        </div>
        <div class="ch-bio">{description_html}{links_html}</div>
        <div class="ch-actions">{subscribe_button}{overflow}</div>
      </section>
      {about_dialog}
      <nav class="ch-tabs" aria-label="Channel sections">
        <div class="ch-tab-row">{tab_html}</div>
        <form class="ch-search{" is-open" if query else ""}" method="get" data-ch-search
              action="/channel/{channel_key}" role="search">
          <button class="ch-search-toggle" type="button" data-ch-search-toggle aria-label="Search this channel"
                  aria-expanded="{"true" if query else "false"}">{_icon("search")}</button>
          <input type="search" name="q" value="{escape(query, quote=True)}" placeholder="Search"
                 aria-label="Search this channel" data-ch-search-input>
        </form>
      </nav>
    '''


def _channel_playlists_html(youtube_channel_id: str) -> str:
    """The Playlists tab. These come from the channel on request and are not stored."""
    try:
        playlists = discovery.fetch_channel_playlists(youtube_channel_id)
    except Exception as exc:
        logger.info("channel playlists failed for %s: %s", youtube_channel_id, exc)
        return _empty_state(
            "playlist", "Playlists could not be loaded", f"The request to this channel failed: {_safe_exc_name(exc)}.",
            f'<a class="btn" href="/channel/{escape(youtube_channel_id, quote=True)}?filter=playlists">Try again</a>',
        )
    if not playlists:
        return _empty_state("playlist", "No public playlists", "This channel has not published any playlists.")
    return f'<div class="result-list ch-playlists">{"".join(_remote_playlist_row(item) for item in playlists)}</div>'


# --- channel tools: remote playlists, the Live tab, bulk channel actions ------------------
# Data and SQL are in channel_tools.py; these render it with the app's own components.


def _remote_playlist_href(playlist_id: str) -> str:
    """Where a YouTube playlist opens in the app: /playlist?list=<id>, as on YouTube. Raw."""
    return f"/playlist?{urllib.parse.urlencode({'list': playlist_id})}"


def _remote_playlist_row(playlist: dict[str, Any]) -> str:
    """A channel's playlist as a row that opens inside OpenFeed."""
    playlist_id = str(playlist.get("playlist_id") or "")
    if not channel_tools.is_remote_playlist_id(playlist_id):
        return _youtube_playlist_row(playlist)
    cover = channel_tools._cover_video_id(playlist.get("thumbnail_url"))
    thumbnail = f"/vi/{cover}/hqdefault.jpg" if cover else ""
    count = playlist.get("video_count")
    parts = [str(playlist.get("author") or ""), _count_text(int(count)) if count else "", "View full playlist"]
    return _playlist_row(
        playlist_id, str(playlist.get("title") or "Playlist"), _remote_playlist_href(playlist_id),
        " · ".join(part for part in parts if part), thumbnail,
    )


def _remote_playlist_rows(videos: list[dict[str, Any]], settings: Any) -> str:
    rows = []
    for video in videos:
        index = video.get("index")
        rows.append(_media_row(
            video, settings,
            index=(int(index) + 1) if isinstance(index, int) else None,
            variant="library",
            filter_text=f"{video.get('title') or ''} {video.get('channel_title') or video.get('author') or ''}",
        ))
    return "".join(rows)


REMOTE_PLAYLIST_FIRST_PAGE = 200
REMOTE_PLAYLIST_MORE_PAGE = 100


@app.get("/playlist", response_class=HTMLResponse, tags=["pages"], summary="A YouTube playlist, opened in the app")
def remote_playlist_page(request: Request):
    """A channel's (or any public) YouTube playlist, read from Invidious and not stored as
    a playlist. Play all and Shuffle run it in list mode; Save to library copies it into a
    playlist of your own."""
    auth_redirect = _login_required_redirect(request)
    if auth_redirect:
        return auth_redirect
    playlist_id = str(request.query_params.get("list") or "").strip()
    if _valid_uuid(playlist_id):
        return RedirectResponse(url=f"/playlist/{playlist_id}", status_code=307)
    if not channel_tools.is_remote_playlist_id(playlist_id):
        return _page_not_found(request, "Playlist not found", "That is not a YouTube playlist link.", "/playlists", "All playlists", "playlists", "playlist")
    settings = get_settings()
    try:
        meta, videos = channel_tools.remote_playlist(playlist_id, REMOTE_PLAYLIST_FIRST_PAGE)
    except channel_tools.RemotePlaylistError as exc:
        return _page_not_found(request, "Playlist not found", f"{exc}.", "/playlists", "All playlists", "playlists", "playlist")
    except Exception as exc:  # noqa: BLE001 - upstream failure is a page, not a 500
        logger.info("remote playlist %s failed: %s", playlist_id, exc)
        retry = f'<a class="btn" href="{escape(_remote_playlist_href(playlist_id), quote=True)}">Try again</a>'
        content = _empty_state("playlist", "This playlist could not be loaded", f"The request to Invidious failed: {_safe_exc_name(exc)}.", retry)
        return _app_shell("Playlist", "playlists", content, request=request)
    _note_catalog(request, videos, "playlist")
    videos = _with_progress(videos, request)
    title_text = meta["title"]
    count = int(meta.get("video_count") or len(videos))
    views = _compact_count(meta.get("view_count"))
    updated = _relative_time(meta.get("updated_at"))
    meta_line = [
        meta.get("author") or "",
        _count_text(count),
        _count_label(views, "view") if views else "",
        f"updated {updated}" if updated else "",
    ]
    cover_id = meta.get("cover_video_id") or (videos[0].get("youtube_video_id") if videos else "")
    cover = escape(f"/vi/{cover_id}/hqdefault.jpg") if cover_id else ""
    save = (
        f'<button class="btn" type="button" data-save-remote-playlist="{escape(playlist_id, quote=True)}" '
        f'data-playlist-title="{escape(title_text, quote=True)}">{_icon("plus")}<span>Save to library</span></button>'
    )
    more = ""
    if len(videos) < count and len(videos) >= REMOTE_PLAYLIST_FIRST_PAGE:
        more = (
            f'<div class="library-more" data-remote-playlist-more-wrap>'
            f'<button class="btn btn-sm" type="button" data-remote-playlist-more="{escape(playlist_id, quote=True)}" '
            f'data-offset="{len(videos)}">Show more</button></div>'
        )
    rows = _remote_playlist_rows(videos, settings)
    items_html = (
        f'<div class="library-list" data-remote-playlist-list>{rows}</div>{more}' if rows
        else _empty_state("playlist", "This playlist is empty", "There is nothing public in it.")
    )
    content = _detail_layout(
        title_text, meta_line, cover, count,
        _play_all_buttons(videos, playlist_id) + save, items_html,
        description=str(meta.get("description") or ""),
        filter_label="Filter this playlist" if rows else "", noun="video",
    )
    return _app_shell(title_text, "playlists", content, request=request)


@app.get("/api/remote-playlists/{playlist_id}/rows", tags=["playlists"], summary="More rows of a YouTube playlist")
def api_remote_playlist_rows(request: Request, playlist_id: str, offset: int = 0) -> dict[str, Any]:
    if not channel_tools.is_remote_playlist_id(playlist_id):
        raise HTTPException(status_code=400, detail="Not a YouTube playlist id")
    try:
        videos, more = channel_tools.remote_playlist_slice(playlist_id, max(0, min(offset, 5000)), REMOTE_PLAYLIST_MORE_PAGE)
    except channel_tools.RemotePlaylistError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Invidious failed: {_safe_exc_name(exc)}") from exc
    _note_catalog(request, videos, "playlist")
    videos = _with_progress(videos, request)
    return {"ok": True, "html": _remote_playlist_rows(videos, get_settings()), "count": len(videos),
            "next_offset": max(0, offset) + len(videos), "more": more}


# How many videos Save to library copies. Invidious pages are ~100 videos each.
REMOTE_PLAYLIST_SAVE_LIMIT = 1000


@app.post("/api/remote-playlists/{playlist_id}/save", tags=["playlists"], summary="Copy a YouTube playlist into your playlists")
def api_save_remote_playlist(request: Request, playlist_id: str) -> dict[str, Any]:
    """A new local playlist with the same name and videos, in order. A copy: later changes
    on YouTube do not follow it."""
    auth_redirect = _login_required_redirect(request)
    if auth_redirect:
        raise HTTPException(status_code=401, detail="Login required")
    if not channel_tools.is_remote_playlist_id(playlist_id):
        raise HTTPException(status_code=400, detail="Not a YouTube playlist id")
    try:
        meta, videos = channel_tools.remote_playlist(playlist_id, REMOTE_PLAYLIST_SAVE_LIMIT)
    except channel_tools.RemotePlaylistError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Invidious failed: {_safe_exc_name(exc)}") from exc
    author = str(meta.get("author") or "")
    description = str(meta.get("description") or "").strip() or (f"Saved from {author} on YouTube." if author else "Saved from YouTube.")
    saved = channel_tools.copy_into_new_playlist(
        meta["title"], description, [str(video.get("youtube_video_id") or "") for video in videos], _user_key(request),
    )
    return {"ok": True, "playlist_id": str(saved["id"]), "href": f"/playlist/{saved['id']}", "added": saved["added"],
            "name": str(saved.get("name") or "")}


def _stream_card_parts(video: dict[str, Any], meta_parts: list[str], duration_html: str) -> tuple[list[str], str]:
    """The Live tab's card: a LIVE badge and "Streamed …" in place of duration and age."""
    kind = video.get("stream_kind")
    if kind == "live":
        return ["Streaming now"], '<span class="duration is-live" aria-label="Live now">LIVE</span>'
    if kind == "upcoming":
        return ["Upcoming"], '<span class="duration is-upcoming">UPCOMING</span>'
    relative = _relative_time(video.get("published_at"))
    return [part if part != relative else f"Streamed {relative}" for part in meta_parts], duration_html


def _live_cards(videos: list[dict[str, Any]], settings: Any, start: int = 1) -> str:
    return "".join(_home_video_card(video, index, settings) for index, video in enumerate(videos, start=start))


def _channel_live_html(youtube_channel_id: str, request: Request, settings: Any) -> str:
    """The Live tab: past and current streams, from the channel on request, live first."""
    channel_key = escape(youtube_channel_id, quote=True)
    try:
        videos, continuation = channel_tools.fetch_channel_streams(youtube_channel_id)
    except Exception as exc:  # noqa: BLE001
        logger.info("channel streams failed for %s: %s", youtube_channel_id, exc)
        return _empty_state(
            "live", "Live streams could not be loaded", f"The request to this channel failed: {_safe_exc_name(exc)}.",
            f'<a class="btn" href="/channel/{channel_key}?filter=live">Try again</a>',
        )
    if not videos:
        return _empty_state("live", "No live streams", "This channel has not streamed yet.")
    channel_tools.store_videos(videos, "channel")
    _note_catalog(request, videos, "channel")
    videos = _with_progress(videos, request)
    more = (
        f'<div class="library-more" data-live-more-wrap><button class="btn btn-sm" type="button" '
        f'data-live-more="{channel_key}" data-continuation="{escape(continuation, quote=True)}">Show more</button></div>'
        if continuation else ""
    )
    return f'''
      <h2 class="sr-only">Live</h2>
      <section class="video-grid" data-live-grid data-channel-id="{channel_key}">{_live_cards(videos, settings)}</section>
      {more}
    '''


@app.get("/api/channels/{youtube_channel_id}/streams/html", tags=["subscriptions"], summary="A channel's live streams as rendered cards")
def api_channel_streams_html(request: Request, youtube_channel_id: str, continuation: str = "") -> dict[str, Any]:
    # YouTube's continuation tokens run past 5,000 characters on big channels, and a cut
    # token is a 400 upstream, so an oversized one is refused rather than truncated.
    if len(continuation) > 16000:
        raise HTTPException(status_code=400, detail="Continuation token too long")
    try:
        videos, next_token = channel_tools.fetch_channel_streams(youtube_channel_id, continuation)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Invidious failed: {_safe_exc_name(exc)}") from exc
    channel_tools.store_videos(videos, "channel")
    _note_catalog(request, videos, "channel")
    videos = _with_progress(videos, request)
    return {"ok": True, "html": _live_cards(videos, get_settings()), "count": len(videos), "continuation": next_token}


class BulkSubscriptionRequest(BaseModel):
    action: str
    subscription_ids: list[str] = Field(default_factory=list, max_length=channel_tools.BULK_LIMIT)
    collection_id: str = ""
    level: str = ""


@app.post("/api/subscriptions/bulk", tags=["subscriptions"], summary="One action over many subscriptions")
def api_bulk_subscriptions(request: Request, payload: BulkSubscriptionRequest) -> dict[str, Any]:
    """The Channels page's select mode: unsubscribe, add to a collection, set the bell
    to all or none, or mute or unmute in the feed, for every selected subscription.

    `applied` lists the subscription ids changed; `skipped` the ones the action does not
    apply to (a feed with no YouTube channel cannot be muted in the feed)."""
    auth_redirect = _login_required_redirect(request)
    if auth_redirect:
        raise HTTPException(status_code=401, detail="Login required")
    action = payload.action.strip().lower()
    if action not in channel_tools.BULK_ACTIONS:
        raise HTTPException(status_code=400, detail="unknown_action")
    if action == "collection":
        _require_collection_id(payload.collection_id)
    try:
        result = channel_tools.apply_bulk_action(
            action, payload.subscription_ids, _user_key(request),
            collection_id=payload.collection_id.strip(), level=payload.level.strip().lower(),
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "action": action, **_json_ready_value(result)}


def _channels_select_bar(collections: list[dict[str, Any]]) -> str:
    """The select mode's action bar on the Channels page. Hidden until Select is pressed."""
    collection_rows = "".join(
        _menu_row("collections", str(item.get("name") or "Untitled collection"),
                  attrs=f' data-bulk-action="collection" data-collection-id="{escape(str(item.get("id") or ""), quote=True)}"')
        for item in collections
    ) or _menu_row("plus", "New collection", href="/channels?tab=collections")
    return f'''
      <div class="bulk-bar" data-bulk-bar hidden role="toolbar" aria-label="Selected channels">
        <label class="bulk-all"><input type="checkbox" data-bulk-all><span>Select all</span></label>
        <p class="bulk-count" data-bulk-count aria-live="polite">0 selected</p>
        <div class="bulk-actions">
          <details class="yt-menu bulk-menu">
            <summary class="btn btn-sm" data-bulk-needs aria-label="Add selected to collection">{_icon("collections")}<span>Add to collection</span></summary>
            <div class="menu-panel" role="menu" aria-label="Collections">{collection_rows}</div>
          </details>
          <details class="yt-menu bulk-menu">
            <summary class="btn btn-sm" data-bulk-needs aria-label="Notifications for selected">{_icon("bell")}<span>Notifications</span></summary>
            <div class="menu-panel" role="menu" aria-label="Notifications">
              {_menu_row("bell-ring", "All", attrs=' data-bulk-action="notify" data-level="all"')}
              {_menu_row("bell-off", "None", attrs=' data-bulk-action="notify" data-level="none"')}
            </div>
          </details>
          <button class="btn btn-sm" type="button" data-bulk-action="mute" data-bulk-needs>{_icon("minus-circle")}<span>Don't recommend</span></button>
          <button class="btn btn-sm btn-danger" type="button" data-bulk-action="unsubscribe" data-bulk-needs>{_icon("x")}<span>Unsubscribe</span></button>
          <button class="btn btn-sm btn-ghost" type="button" data-bulk-done>Done</button>
        </div>
      </div>
    '''


def _channels_select_toggle() -> str:
    return f'<button class="btn btn-sm" type="button" data-bulk-toggle aria-pressed="false">{_icon("check")}<span>Select</span></button>'


def _subscriptions_with_bell(subscriptions: list[dict[str, Any]], levels: dict[str, str]) -> list[dict[str, Any]]:
    """Rows annotated with `notify_key` and its level, for every subscription.

    A YouTube channel's bell is keyed by its channel id. A feed with no channel id (a
    playlist feed, an imported RSS feed) used to get no bell at all; it is keyed by
    channel_tools.feed_notification_key instead.
    """
    rows = []
    for item in subscriptions:
        key = channel_tools.notification_key(item)
        if channel_tools.can_notify(item):
            rows.append({**item, "notify_key": key, "notify_level": levels.get(key, "all")})
        else:
            # OpenFeed keeps only YouTube videos from a feed. One that has never delivered
            # any (a GitHub releases feed) has nothing a bell could announce, so it gets
            # no bell and says why rather than offering a control that does nothing.
            rows.append({**item, "notify_level": None, "sort_note": item.get("sort_note") or "No YouTube videos in this feed"})
    return rows


def _with_feed_uploads(rows: list[dict[str, Any]], limit: int, user_key: str, include_shorts: bool) -> list[dict[str, Any]]:
    """Channel uploads plus videos from subscribed feeds with no channel, newest first."""
    try:
        extra = channel_tools.list_feed_uploads(limit, user_key, include_shorts)
    except Exception as exc:  # noqa: BLE001 - the channel uploads still notify
        logger.info("feed uploads unavailable: %s", exc)
        return rows
    if not extra:
        return rows
    seen = {str(row.get("youtube_video_id") or "") for row in rows}
    merged = rows + [row for row in extra if str(row.get("youtube_video_id") or "") not in seen]
    epoch = datetime.fromtimestamp(0, tz=timezone.utc)
    return sorted(merged, key=lambda row: row.get("published_at") or epoch, reverse=True)


def _shorts_subscribe_button(video: dict[str, Any]) -> str:
    """The Subscribe pill beside the channel on a Short, as on YouTube Shorts.

    The shared subscribe handler (data-subscribe-feed / data-unsubscribe-feed) does the
    work; channel-tools.js keeps every Short from the same channel in step.
    """
    channel_id_raw = str(video.get("youtube_channel_id") or "")
    if not channel_id_raw:
        return ""
    channel_text = str(video.get("channel_title") or video.get("author") or "this channel")
    channel_id = escape(channel_id_raw, quote=True)
    feed_url = escape(_youtube_feed_url(channel_id_raw), quote=True)
    title_attr = escape(channel_text, quote=True)
    if video.get("channel_subscribed"):
        return (
            f'<button class="shorts-subscribe subscribed" type="button" data-unsubscribe-feed data-shorts-subscribe '
            f'data-channel-id="{channel_id}" data-feed-url="{feed_url}" data-title="{title_attr}" '
            f'aria-label="Unsubscribe from {title_attr}"><span>Subscribed</span></button>'
        )
    return (
        f'<button class="shorts-subscribe" type="button" data-subscribe-feed data-shorts-subscribe '
        f'data-channel-id="{channel_id}" data-feed-url="{feed_url}" data-title="{title_attr}" '
        f'aria-label="Subscribe to {title_attr}"><span>Subscribe</span></button>'
    )


def _with_subscription_state(videos: list[dict[str, Any]], user_key: str) -> list[dict[str, Any]]:
    """Mark each video whose channel this user follows (`channel_subscribed`). One query."""
    try:
        subscribed = {str(row.get("youtube_channel_id") or "").lower() for row in db.list_subscriptions(1000, user_key=user_key)}
    except Exception as exc:  # noqa: BLE001 - the button then offers Subscribe, which is harmless
        logger.info("subscription state unavailable: %s", exc)
        return videos
    for video in videos:
        video["channel_subscribed"] = str(video.get("youtube_channel_id") or "").lower() in subscribed
    return videos


def _short_card_menu(video_id: str, title_text: str) -> str:
    """The ⋮ on a Home Shorts shelf card. video_id is already escaped."""
    title = escape(_text_excerpt(title_text, 72))
    rows = [
        _menu_row("playlist", "Save to playlist", attrs=f' data-save-to-playlist="{video_id}" data-video-title="{escape(_text_excerpt(title_text, 90), quote=True)}"'),
        _menu_row("share", "Share", attrs=f' data-copy-url="/shorts?start={video_id}"'),
        '<div class="menu-separator" role="separator"></div>',
        _menu_row("block", "Not interested", attrs=f' data-feedback="hide" data-video-id="{video_id}"', danger=True),
    ]
    return (
        f'<details class="card-menu yt-menu short-card-menu">'
        f'<summary class="menu-trigger" aria-label="Open actions for {title}">{_icon("more")}</summary>'
        f'<div class="menu-panel" role="menu" aria-label="Short actions">{"".join(rows)}</div></details>'
    )


def _shorts_slide_menu(video_id: str, title_text: str) -> str:
    """The ⋮ at the foot of a Short's action rail. video_id is already escaped."""
    title = escape(_text_excerpt(title_text, 72))
    row = _menu_row("block", "Not interested", attrs=f' data-shorts-not-interested data-video-id="{video_id}"', danger=True)
    return (
        f'<details class="yt-menu shorts-more">'
        f'<summary class="shorts-action" aria-label="More actions for {title}"><span class="shorts-disc">{_icon("more")}</span></summary>'
        f'<div class="menu-panel" role="menu" aria-label="More actions">'
        f'{row}'
        f'</div></details>'
    )


def _shorts_from_scan(rows: list[dict[str, Any]], limit: int) -> tuple[list[dict[str, Any]], int]:
    """The first `limit` Shorts in a scan of a channel, and how many rows that used.

    Channel paging offsets count every stored video, Shorts or not, so the offset the next
    request starts from is the rows consumed, never the number of Shorts kept: the same
    rows would come back otherwise. Stopping at the last row kept leaves no gap either.
    """
    shorts: list[dict[str, Any]] = []
    for position, row in enumerate(rows, start=1):
        if db.is_short_video(row):
            shorts.append(row)
            if len(shorts) >= limit:
                return shorts, position
    return shorts, len(rows)


@app.get("/channel/{youtube_channel_id}", response_class=HTMLResponse)
def channel_page(request: Request, youtube_channel_id: str, limit: int = 80, sort: str = "latest", filter: str = "", q: str = ""):
    auth_redirect = _login_required_redirect(request)
    if auth_redirect:
        return auth_redirect
    if "q" in request.query_params and not (q or "").strip():
        # An empty channel search is no search: drop the bare ?q= rather than keep it in the URL.
        kept = urllib.parse.urlencode([(k, v) for k, v in request.query_params.multi_items() if k != "q"])
        return RedirectResponse(url=f"{request.url.path}{'?' + kept if kept else ''}", status_code=303)
    channel_viewer = _user_key(request)
    channel = db.get_channel_by_youtube_id(youtube_channel_id, channel_viewer)
    if not channel:
        return _page_not_found(request, "Channel not found", "OpenFeed has no channel with this ID. The link may be wrong, or the channel was never added here.", "/channels", "All channels", "channels", "channels")
    # A channel that only came from a search has no description, banner or tab list yet,
    # and the worker may be hours away. enrich_channel swallows its own failures.
    if not channel.get("available_tabs") and not channel.get("description"):
        enrich_channel(youtube_channel_id)
        channel = db.get_channel_by_youtube_id(youtube_channel_id, channel_viewer) or channel

    settings = get_settings()
    sort_key = sort if sort in {"latest", "popular", "oldest"} else "latest"
    bounded_limit = max(1, min(limit, 500))
    kind = filter if filter in {"shorts", "playlists", "live"} else ""
    query = (q or "").strip()
    # Search the whole stored channel in SQL, not just the first page in Python.
    # The old in-memory filter made "no results" mean "no results in the first 80".
    fetch_limit = bounded_limit * 3 if kind == "shorts" and not query else bounded_limit
    videos = db.list_channel_videos(youtube_channel_id, fetch_limit, sort_key, 0, query) if kind not in {"playlists", "live"} else []
    if not query and len(videos) < bounded_limit and kind not in {"playlists", "live"}:
        # First paint was capped at whatever RSS/sync had stored (~15 videos).
        # Top up from the channel itself so the page reflects the channel, not the cache.
        try:
            if _ingest_channel_uploads(youtube_channel_id, bounded_limit, sort_key, max_pages=3):
                videos = db.list_channel_videos(youtube_channel_id, fetch_limit, sort_key, 0, query)
        except Exception as exc:
            logger.info("channel backfill failed for %s: %s", youtube_channel_id, exc)
    if kind == "shorts":
        videos, next_offset = _shorts_from_scan(videos, bounded_limit)
    else:
        videos = videos[:bounded_limit]
        next_offset = len(videos)
    # The channel's own upload list is public and read from the shared catalog; what this
    # person was shown of it becomes theirs to be offered elsewhere (catalog_scope.py).
    _note_catalog(request, videos, "channel")
    title_text = str(channel.get("title") or "Unknown channel")
    channel_key = escape(youtube_channel_id, quote=True)
    videos = _with_progress(videos, request)
    video_grid = "".join(_home_video_card(video, index, settings) for index, video in enumerate(videos, start=1))
    if not video_grid:
        video_grid = (
            _empty_state("search", f"No results for \u201c{query}\u201d", "Nothing stored from this channel matches. Try different words.") if query
            else _empty_state("shorts", "No Shorts from this channel yet", "Shorts appear here once they have been fetched from the channel.") if kind == "shorts"
            else _empty_state("play", "No videos from this channel yet", "Videos appear here after the next sync.")
        )
    # Sorting is a filter on one tab, not a tab of its own — the old page presented
    # Latest/Popular/Oldest as top-level tabs alongside nothing else, which made the page
    # look like it had three sections when it has one.
    kind_param = "filter=shorts&" if kind == "shorts" else ""
    filter_html = "".join(
        f'<a class="ch-chip{" active" if sort_key == key else ""}" '
        f'href="/channel/{channel_key}?{kind_param}sort={key}&limit={bounded_limit}"'
        f'{" aria-current=\"page\"" if sort_key == key else ""}>{label}</a>'
        for key, label in (("latest", "Latest"), ("popular", "Popular"), ("oldest", "Oldest"))
    )
    active_tab = kind or "videos"
    # The channel's playlists are listed outright, with no video grid, so no scroll hooks.
    if kind == "playlists":
        tab_body = _channel_playlists_html(youtube_channel_id)
    elif kind == "live":
        tab_body = _channel_live_html(youtube_channel_id, request, settings)
    elif kind == "shorts" and _shorts_hidden(_user_key(request)):
        tab_body = _shorts_off_state(f"/channel/{channel_key}?filter=shorts&settings=content")
    else:
        tab_body = f'''
      {f'<div class="ch-filters">{filter_html}</div>' if not query else ""}
      <h2 class="sr-only">{"Shorts" if kind == "shorts" else "Videos"}</h2>
      <section class="video-grid" data-channel-grid data-channel-id="{channel_key}"
               data-sort="{sort_key}" data-kind="{kind or "videos"}" data-q="{escape(query, quote=True)}" data-next-offset="{next_offset}">{video_grid}</section>
      {_feed_loading("Loading more videos", "data-channel-loading", f'<div class="video-grid">{_skeleton_cards(4)}</div>')}
      <div class="feed-sentinel" data-channel-sentinel aria-hidden="true"></div>
        '''

    content = f'''
      <div class="ch-wrap">
      {_channel_head(channel, youtube_channel_id, active_tab, _user_key(request), query, len(videos))}
      {tab_body}
      </div>
    '''
    return _app_shell(title_text, "channels", content, main_class="channel-main", request=request)


@app.get("/api/videos")
def api_videos(request: Request, limit: int = 50) -> dict[str, Any]:
    """Recent videos. A member sees uploads from their own subscriptions: the full catalog
    is everything every account on this server followed, searched and watched."""
    capped = max(1, min(limit, 200))
    if policy.is_member(_current_user(request)):
        return {"items": _json_ready(db.list_subscription_uploads(capped, _user_key(request)))}
    # An admin too sees only their own part of the catalog: the rest would show what other
    # accounts searched for and opened.
    return {"items": _json_ready(db.list_recent_videos(capped, _user_key(request)))}


@app.get("/api/search")
def api_search(request: Request, q: str = "", limit: int = 50, source: str = "all", page: int = 1) -> dict[str, Any]:
    """Search YouTube and the local library.

    `source=library` restricts it to what is already stored, which is what this endpoint
    used to do unconditionally. `page` pages the YouTube side (1-5); the library side is
    always the top matches. Each side gets its own `limit` so a full library page can
    no longer starve YouTube results to zero.
    """
    capped = max(1, min(limit, 200))
    page_num = max(1, min(int(page or 1), 5))
    # The library side is the caller's part of the shared catalog (catalog_scope.py).
    user_key = _search_user_key(request)
    local = db.search_videos(q, capped, user_key=user_key)
    if source == "library" or not q.strip():
        return {"items": _json_ready(local), "source": "library"}

    known = {str(video.get("youtube_video_id") or "") for video in local}
    remote: list[dict[str, Any]] = []
    error = ""
    try:
        remote = [item for item in discovery.search_youtube(q, page=page_num) if item.get("youtube_video_id") not in known]
        if remote:
            db.store_search_results(remote[:capped], q, user_key)
    except Exception as exc:
        error = _safe_exc_name(exc)
        logger.warning("youtube search failed for %s: %s", privacy.loggable(q), exc)
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
def api_search_html(request: Request, q: str = "", limit: int = 40, page: int = 2, type: str = "all", sort: str = "relevance", date: str = "", duration: str = "", features: str = "") -> dict[str, Any]:
    """The next pages of a search, already rendered.

    Returns markup for the same reason the channel feed does: the row is rendered
    server-side, and a second implementation of it in JavaScript would drift from
    this one.

    Page 1 is the search page itself, which shows every library match. Deeper
    pages are YouTube-only (`page` pages the YouTube side, 1-5), with anything
    already stored — including the page-1 YouTube rows, which are stored on
    sight — left out so scrolling never repeats a video.

    `type`, `sort`, `date`, `duration` and `features` mirror the page the list was rendered
    from. Without them, scrolling a filtered search would append unfiltered rows under it.
    """
    settings = get_settings()
    cleaned = q.strip()
    capped = max(1, min(limit, 100))
    page_num = max(1, min(int(page or 1), 5))
    if not cleaned:
        return {"ok": True, "html": "", "count": 0, "next_page": page_num, "has_more": False}
    filters = _search_filters(type, sort, date, duration, features)
    _search_rating_gate(filters, _search_user_key(request))
    if filters["kind"] == "channels":
        found, error, more = _search_channels(cleaned, capped, page_num, _search_user_key(request))
        return {
            "ok": True, "html": _channel_results_html(found, _user_key(request)), "count": len(found),
            "next_page": page_num + (0 if error else 1), "has_more": page_num < 5 and more and not error,
        }
    if filters["kind"] == "playlists":
        rows, count, error, more = _search_playlists(cleaned, _user_key(request), page_num)
        return {
            "ok": True, "html": rows, "count": count,
            "next_page": page_num + (0 if error else 1), "has_more": page_num < 5 and more and not error,
        }
    user_key = _search_user_key(request)
    local = _search_local_videos(cleaned, capped, filters, user_key) if page_num == 1 else []
    try:
        remote, scanned = _search_youtube_videos(cleaned, page_num, filters)
    except Exception as exc:
        logger.warning("youtube search failed for %s (page %d): %s", privacy.loggable(cleaned), page_num, exc)
        return {"ok": True, "html": "", "count": 0, "next_page": page_num, "has_more": False}

    if page_num == 1:
        # Exactly what the search page shows: the library matches first, then
        # YouTube with those left out.
        known = {str(video.get("youtube_video_id") or "") for video in local}
    else:
        # Page 1 already showed the top library matches; everything stored —
        # library videos and the stored page-1 YouTube rows — stays off.
        known = db.video_ids_in_library([item.get("youtube_video_id") for item in remote], user_key)
    fresh = [item for item in remote if item.get("youtube_video_id") not in known]
    if fresh:
        try:
            db.store_search_results(fresh[:capped], cleaned, user_key)
        except Exception as exc:
            logger.warning("could not store search results for %s: %s", privacy.loggable(cleaned), exc)

    shown = (local[:capped] if page_num == 1 else []) + fresh[:capped]
    if filters["sort"] in {"view_count", "upload_date"}:
        shown = _order_search_page(shown, filters["sort"])
    elif filters["sort"] == "rating":
        shown = _order_by_rating(shown)
    shown = _with_progress(shown, request)
    html = "".join(
        _media_row(video, settings, "In your library" if video.get("youtube_video_id") in known else "From YouTube")
        for video in shown
    )
    return {
        "ok": True,
        "html": html,
        "count": len(shown),
        "next_page": page_num + 1,
        # Scanned rather than kept: a Shorts page can filter down to nothing with more behind it.
        "has_more": page_num < 5 and bool(scanned if filters["kind"] != "all" else fresh),
    }


# ---- search type-ahead, Watch later, feedback undo and the Not interested list ----
# The logic lives in search_extras.py; these are the routes and the page.


@app.get("/api/search/suggestions", tags=["search"], summary="Type-ahead suggestions for the search box")
def api_search_suggestions(request: Request, q: str = "") -> JSONResponse:
    """YouTube's suggestions for what has been typed so far, through this deployment's Invidious.

    Recent searches are kept in the browser, so the search box puts its own matches first.
    Cached for ten minutes per prefix and per account on the server, and briefly in the browser.
    """
    scope = policy.account_key(_current_user(request)) if policy.shared_install() else ""
    items = search_extras.suggestions(q, scope=scope)
    return JSONResponse({"query": q.strip(), "suggestions": items}, headers={"Cache-Control": "private, max-age=300"})


class WatchLaterRequest(BaseModel):
    youtube_video_id: str
    saved: bool = True


@app.get("/api/watch-later", tags=["playlists"], summary="Video ids in Watch later")
def api_watch_later(request: Request) -> dict[str, Any]:
    """The ids in the caller's Watch later playlist, so cards can show which are saved."""
    user_key = _user_key(request)
    playlist = search_extras.watch_later_playlist(user_key)
    return {"ok": True, "playlist_id": str(playlist["id"]) if playlist else "", "ids": search_extras.watch_later_ids(user_key)}


@app.post("/api/watch-later", tags=["playlists"], summary="Save a video to Watch later or take it out")
def api_set_watch_later(request: Request, payload: WatchLaterRequest) -> dict[str, Any]:
    """One-tap Watch later. Watch later is the playlist of that name, made on first save."""
    video_id = payload.youtube_video_id.strip()
    if not ryd.valid_video_id(video_id):
        raise HTTPException(status_code=400, detail="invalid_video_id")
    result = search_extras.set_watch_later(video_id, payload.saved, _user_key(request))
    if result is None:
        raise HTTPException(status_code=404, detail="Video not found")
    return {"ok": True, "youtube_video_id": video_id, **result}


class FeedbackUndoRequest(BaseModel):
    youtube_video_id: str
    event_id: int
    previous: dict[str, Any] = Field(default_factory=dict)


@app.post("/api/feedback/undo", tags=["recommendations"], summary="Undo More like this, Less like this or Not interested")
def api_feedback_undo(request: Request, payload: FeedbackUndoRequest) -> dict[str, Any]:
    """The toast's Undo. Removes the recorded event and puts the video's feedback back the
    way POST /api/feedback reported it was (`previous`)."""
    video_id = payload.youtube_video_id.strip()
    user_key = _user_key(request)
    if not search_extras.undo_feedback(video_id, payload.event_id, payload.previous, user_key):
        raise HTTPException(status_code=404, detail="Nothing to undo")
    db.clear_watch_time_cache(user_key)
    return {"ok": True, "feedback": _json_ready_value(db.get_video_feedback(video_id, user_key) or {})}


@app.delete("/api/not-interested/{youtube_video_id}", tags=["recommendations"], summary="Let a video back into recommendations")
def api_clear_not_interested(request: Request, youtube_video_id: str) -> dict[str, Any]:
    """Removes a video from the Not interested list: Not interested and Less like this both go."""
    user_key = _user_key(request)
    if not search_extras.clear_not_interested(youtube_video_id.strip(), user_key):
        raise HTTPException(status_code=404, detail="Not in the list")
    db.clear_watch_time_cache(user_key)
    return {"ok": True}


def _not_interested_channel_row(channel: dict[str, Any]) -> str:
    key = str(channel.get("key") or "")
    name = str(channel.get("title") or key)
    channel_id = str(channel.get("youtube_channel_id") or "")
    href = f"/channel/{urllib.parse.quote(channel_id, safe='')}" if channel_id else ""
    avatar = _avatar_html(name, channel.get("avatar_url"), tag="a" if href else "span", href=href)
    title_html = f'<a href="{escape(href, quote=True)}">{escape(name)}</a>' if href else escape(name)
    label = f"Remove from Not interested: {name}"
    return f'''
    <article class="media-row library-row interest-channel" data-filter-item="{escape(name, quote=True)}" data-interest-row>
      {avatar}
      <div class="media-row-copy">
        <h2>{title_html}</h2>
        <p class="row-byline"><span class="row-meta">Don&rsquo;t recommend channel</span></p>
      </div>
      <div class="row-actions">{_row_remove(f'data-unmute-interest="{escape(key, quote=True)}" data-channel-title="{escape(name, quote=True)}"', label)}</div>
    </article>
    '''


def _not_interested_body(videos: list[dict[str, Any]], channels: list[dict[str, Any]], settings: Any) -> str:
    if not videos and not channels:
        return _empty_state(
            "block", "Nothing here",
            "Videos you mark Not interested or Less like this, and channels you choose Don't recommend for, are listed here so you can let them back in.",
            '<a class="btn btn-sm" href="/">Back to Home</a>',
        )
    video_rows = []
    for video in videos:
        video_id = escape(str(video.get("youtube_video_id") or ""), quote=True)
        title_text = str(video.get("title") or "Untitled")
        kind = "Not interested" if video.get("not_interested") else "Less like this"
        marked = _relative_time(video.get("marked_at"))
        hooks = f'data-restore-video="{video_id}"'
        video_rows.append(_media_row(
            video, settings,
            trailing=_row_remove(hooks, f"Remove from Not interested: {title_text}"),
            meta=[f"{kind} · {marked}" if marked else kind],
            menu_rows=_menu_row("x", "Remove from Not interested", attrs=f" {hooks}"),
            variant="library",
            filter_text=f"{title_text} {video.get('channel_title') or video.get('author') or ''}",
        ))
    sections = []
    if video_rows:
        sections.append(
            f'<section class="interest-section" aria-labelledby="interest-videos"><h2 class="search-section-title" id="interest-videos">Videos</h2>'
            f'<div class="library-list">{"".join(video_rows)}</div></section>'
        )
    if channels:
        sections.append(
            f'<section class="interest-section" aria-labelledby="interest-channels"><h2 class="search-section-title" id="interest-channels">Channels</h2>'
            f'<div class="library-list">{"".join(_not_interested_channel_row(channel) for channel in channels)}</div></section>'
        )
    return _library_filter("Filter this list") + "".join(sections) + _filter_empty("item")


@app.get("/not-interested", response_class=HTMLResponse, tags=["pages"], summary="Videos and channels you asked to see less of")
def not_interested_page(request: Request):
    """Not interested, Less like this and Don't recommend channel, each one removable."""
    auth_redirect = _login_required_redirect(request)
    if auth_redirect:
        return auth_redirect
    user_key = _user_key(request)
    videos = search_extras.list_not_interested_videos(user_key)
    channels = search_extras.muted_channel_rows(user_key)
    total = len(videos) + len(channels)
    meta = _count_text(total, "item") if total else ""
    return _library_page_shell(request, "Not interested", _not_interested_body(videos, channels, get_settings()), "not-interested", meta)


@app.get("/api/recommendations")
def api_recommendations(request: Request, limit: int = 50, offset: int = 0, filter: str = "all") -> dict[str, Any]:
    bounded_limit = max(1, min(limit, 200))
    bounded_offset = max(0, offset)
    return {"items": _json_ready(db.list_recommended_videos(bounded_limit, bounded_offset, filter, _user_key(request)))}


@app.get("/api/home/sections")
def api_home_sections(request: Request, page: int = 0, filter: str = "all") -> dict[str, Any]:
    settings = get_settings()
    html = _home_feed_sections_html(max(0, page), filter, settings, _user_key(request))
    empty_html = _home_empty_state(_home_filter_key(filter)) if page <= 0 and not html.strip() else ""
    return {"ok": True, "html": html, "empty_html": empty_html, "next_page": max(0, page) + 1, "has_more": bool(html.strip())}


def _ingest_channel_uploads(youtube_channel_id: str, needed: int, sort_key: str = "latest", max_pages: int = 6) -> int:
    """Pull a channel's uploads into the library until its first `needed` are stored.

    Returns how many videos were new. Upstream pages follow Invidious's continuation chain
    and are cached briefly (discovery.fetch_channel_uploads), so scrolling costs one
    upstream request per new page and re-opening a channel costs none. Only videos not
    already stored are written, in one batch: rewriting every known row on every visit was
    most of the 3-5 s a channel page and each scroll batch used to take.
    """
    sort_map = {"latest": "newest", "oldest": "oldest", "popular": "popular"}
    upstream_sort = sort_map.get(sort_key, "newest")
    stored = 0
    walked = 0
    for page in range(1, max(1, max_pages) + 1):
        items = discovery.fetch_channel_uploads(youtube_channel_id, page, sort_by=upstream_sort)
        if not items:
            break
        walked += len(items)
        known = db.video_ids_in_library([item.get("youtube_video_id") for item in items])
        fresh = [item for item in items if item.get("youtube_video_id") and item.get("youtube_video_id") not in known]
        if fresh:
            stored += db.upsert_discovered_videos(fresh, "channel")
        if walked >= needed:
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
    # advances by what it consumed (not the filtered count): no gaps and no duplicates
    # at the boundaries.
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
        videos, consumed = _shorts_from_scan(videos, size)
        next_offset = start + consumed
        # A fetch that left nothing at this offset is the end, however many rows it stored.
        has_more = consumed < scanned or scanned >= window or (fetched > 0 and scanned > 0)
    else:
        next_offset = start + len(videos)
        has_more = len(videos) >= size

    _note_catalog(request, videos, "channel")
    videos = _with_progress(videos, request)
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
def api_evaluate_recommendations(
    request: Request, min_videos: int = 5, max_channels: int = 0, classic: bool = True, depth: int = 100
) -> dict[str, Any]:
    """Leave-one-subscribed-channel-out quality, plus coverage of the live feed.

    Each subscribed channel with at least `min_videos` videos is dropped from the seeds in
    turn; the ranker then ranks the whole library and its videos are the positives.
    Reports nDCG@20, recall@100 and the random expectation for graph and blend, and for
    classic when `classic` is true (classic cannot drop the channel, so it is not a clean
    leave-one-out). `max_channels` caps the run (0 = every channel). Coverage reports
    what the feed is made of, because a ranker can score well while showing four creators.
    """
    auth_redirect = _login_required_redirect(request)
    if auth_redirect:
        raise HTTPException(status_code=401, detail="Login required")
    from . import evaluation

    return evaluation.full_report(
        _user_key(request),
        min_videos=max(2, min(min_videos, 100)),
        max_channels=max(0, min(max_channels, 500)),
        include_classic=classic,
        depth=max(20, min(depth, 300)),
    )


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
            stored = _subscribe_channel_local(channel_id, title, feed_url, user_key=user_key)
        except Exception as exc:  # noqa: BLE001 - one bad channel must not stop the batch
            message = _safe_exc_name(exc)
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
    # Growing the library and rebuilding topic vectors work on the whole install. On a
    # shared install only an admin starts them; a member's refresh rebuilds their own
    # profile from what is there.
    if policy.is_member(_current_user(request)):
        grow = vectors = False
        result["install_wide"] = "skipped: admin only"
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


@app.get("/api/dearrow/branding", tags=["playback"], summary="DeArrow titles and thumbnails")
def api_dearrow_branding(request: Request, ids: str = "") -> dict[str, Any]:
    """Community titles, and frame URLs where a frame has been cut, for up to 120 videos.

    The browser never talks to DeArrow. This server asks by four-character hash prefix,
    caches each bucket, and answers only for the ids asked about. A video with no trusted
    submission is simply absent. Frames that are not cut yet are queued and appear on a
    later request.
    """
    from . import dearrow

    video_ids = [part for part in re.split(r"[\s,]+", ids or "") if part][: dearrow.MAX_LOOKUP_IDS]
    return dearrow.branding_for(video_ids, _user_key(request))


@app.get("/api/dearrow/thumbnail/{youtube_video_id}.jpg", tags=["playback"], summary="A DeArrow thumbnail frame cut on this server")
def api_dearrow_thumbnail(request: Request, youtube_video_id: str) -> Response:
    from . import dearrow

    if not dearrow.enabled(_user_key(request)):
        raise HTTPException(status_code=404, detail="dearrow_disabled")
    frame = dearrow.current_frame(youtube_video_id)
    if not frame:
        raise HTTPException(status_code=404, detail="no_frame")
    return FileResponse(frame, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


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
    muted_keys = [str(c).lower() for c in (db.get_feed_preferences(user_key).get("muted_channels") or [])]
    muted = set(muted_keys)
    channels = [{**row, "muted": str(row["key"]).lower() in muted} for row in db.top_feed_channels(user_key, 14)]
    # A muted creator is excluded from the feed, so it never appears among the creators
    # filling it. Without these rows its chip vanished, there was no way to unmute it, and
    # the next mute saved only the visible chips, silently unmuting it.
    shown = {str(row["key"]).lower() for row in channels}
    missing = [key for key in dict.fromkeys(muted_keys) if key not in shown]
    if missing:
        titles = db.channel_titles_for_keys(missing)
        channels += [{"key": key, "title": titles.get(key, key), "count": 0, "muted": True} for key in missing]
    return {"ok": True, "coverage": coverage, "channels": channels}


class MutedChannelRequest(BaseModel):
    channel_key: str
    muted: bool = True


@app.post("/api/feed/muted-channels", tags=["recommendations"], summary="Mute or unmute one creator")
def api_set_channel_muted(request: Request, payload: MutedChannelRequest) -> dict[str, Any]:
    """"Don't recommend channel" and its Undo. Changes one key of muted_channels."""
    key = payload.channel_key.strip()
    if not key or len(key) > 200:
        raise HTTPException(status_code=400, detail="channel_key_required")
    muted = db.set_channel_muted(key, payload.muted, _user_key(request))
    return {"ok": True, "channel_key": key.lower(), "muted": key.lower() in muted, "muted_channels": muted}


class HiddenShelfRequest(BaseModel):
    shelf_key: str
    hidden: bool = True


@app.post("/api/feed/hidden-shelves", tags=["recommendations"], summary="Hide one Home shelf or show it again")
def api_set_shelf_hidden(request: Request, payload: HiddenShelfRequest) -> dict[str, Any]:
    """"Hide this shelf" on Home, its Undo, and Show under Feed controls → Hidden shelves."""
    key = payload.shelf_key.strip().lower()
    if key not in _HIDEABLE_SHELVES:
        raise HTTPException(status_code=400, detail="unknown_shelf")
    hidden = db.set_shelf_hidden(key, payload.hidden, _user_key(request))
    return {"ok": True, "shelf_key": key, "hidden": key in hidden, "hidden_shelves": hidden}


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
        dearrow_enabled=keep(payload.dearrow_enabled, "dearrow_enabled"),
        dearrow_casing=keep(payload.dearrow_casing, "dearrow_casing"),
        dearrow_thumbnails=keep(payload.dearrow_thumbnails, "dearrow_thumbnails"),
        show_posts_in_feeds=keep(payload.show_posts_in_feeds, "show_posts_in_feeds"),
        muted_channels=keep(payload.muted_channels, "muted_channels"),
        exclude_shorts=keep(payload.exclude_shorts, "exclude_shorts"),
        discovery_level=keep(payload.discovery_level, "discovery_level"),
        setup_dismissed=keep(payload.setup_dismissed, "setup_dismissed"),
        ranker_mode=keep(payload.ranker_mode, "ranker_mode"),
        explore_enabled=keep(payload.explore_enabled, "explore_enabled"),
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
    start = max(0, min(int(t or 0), 86400))
    quality = escape(str(record.get("quality") or ""))
    note = f"Downloaded &middot; {quality}" if quality else "Downloaded"
    return _native_player_page(f"/api/downloads/{video_id}/file#t={start}", note, autoplay == "1")


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
    quality = ""
    try:
        quality = escape(str(ytdlp.playback_stream(youtube_video_id).get("quality") or ""))
    except Exception as exc:
        # No <video> in this document, which is what tells the shell to report the failure
        # and move on to the next source.
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
    return _native_player_page(f"/api/ytdlp/stream/{video_id}#t={start}", note, autoplay == "1")


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

    # Through OPENFEED_OUTBOUND_PROXY when set: the signed URL is bound to the address
    # yt-dlp extracted it from, and yt-dlp used that proxy too.
    client = outbound.media_client()
    # The pooled client does not follow redirects, which is right for Invidious assets but
    # wrong here: googlevideo answers with a 302 to a different host, and handing that back
    # would send the browser straight to Google — leaking the address this proxy exists to
    # keep private, while looking like it worked.
    # The stream URL comes from extraction output; if a hostile backend ever pointed it
    # at a loopback/private address this proxy would become a read-SSRF into the stack.
    try:
        netguard.assert_public_url(str(stream["stream_url"]))
    except netguard.UnsafeExternalUrl as exc:
        logger.warning("yt-dlp stream for %s pointed at an unsafe host: %s", youtube_video_id, exc)
        raise HTTPException(status_code=502, detail="The stream host is not a public media endpoint") from exc
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
            # "Public" instances are operator/user-chosen, so never let one probe the
            # loopback/private network — including via redirects to internal hosts.
            with outbound.client(timeout=httpx.Timeout(8.0, connect=4.0)) as probe_client:
                response = netguard.safe_get(probe_client, url)
            ok = response.status_code == 200
            detail = "" if ok else f"HTTP {response.status_code}"
        except netguard.UnsafeExternalUrl as exc:
            logger.info("playback probe refused unsafe source %s: %s", source["id"], exc)
            ok, detail = False, "unsafe source URL"
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


# What an Invidious error page says when the fault really is YouTube refusing the request.
_YOUTUBE_REFUSAL = re.compile(r"sign in|\bbot\b|token|attestation|could not extract|login|reload", re.IGNORECASE)


def _invidious_error_text(page: str) -> str:
    """The sentence on an Invidious error page, or "" when this is not one."""
    if "Error - Invidious" not in page:
        return ""
    match = re.search(r'<div class="h-box">\s*([^<\n]+)', page)
    return unescape(match.group(1)).strip()[:200] if match else ""


def _diagnose_playback(video_id: str) -> dict[str, Any]:
    """Walk the playback chain and report the first hop that breaks.

    Every failure mode below produced a real outage on this deployment, so the messages
    name the actual cause instead of the generic "something went wrong" the player shows.
    """
    settings = get_settings()
    steps: list[dict[str, Any]] = []
    hop_started = time.perf_counter()

    def step(name: str, ok: bool, detail: str = "") -> None:
        nonlocal hop_started
        now = time.perf_counter()
        steps.append({"name": name, "ok": ok, "detail": detail, "latency_ms": round((now - hop_started) * 1000)})
        hop_started = now

    if not settings.invidious_base_url:
        return {"ok": False, "code": "not-configured", "steps": steps,
                "headline": "No Invidious backend is configured.",
                "explanation": "OpenFeed has no INVIDIOUS_BASE_URL, so the private playback path does not exist.",
                "remedies": ["Set INVIDIOUS_BASE_URL and restart OpenFeed.", _fallback_remedy("Until then, you can turn on the YouTube player under Settings > Privacy. Google then sees your IP address and what you watch.")]}

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
                "remedies": ["Wait about a minute and try again.", "OpenFeed tries your other sources in the meantime."]}

    if embed.status_code != 200 or "<video" not in embed.text:
        reason = _invidious_error_text(embed.text)
        step("Player page", False, f"HTTP {embed.status_code}: {reason}" if reason else f"HTTP {embed.status_code}")
        if reason and not _YOUTUBE_REFUSAL.search(reason):
            return {"ok": False, "code": "video-unavailable", "steps": steps,
                    "headline": f"Invidious cannot open this video: {reason}.",
                    "explanation": f"Its player page answered HTTP {embed.status_code} with that reason, so no stream was requested.",
                    "remedies": ["If the reason is about the video itself, no source on this server can play it. Open it on YouTube to confirm."]}
        if not reason:
            return {"ok": False, "code": "no-player-page", "steps": steps,
                    "headline": "Invidious did not return a player page.",
                    "explanation": f"Its player page answered HTTP {embed.status_code} and gave no reason.",
                    "remedies": ["Look at the openfeed-invidious logs.", "Try again in a moment."]}
        return {"ok": False, "code": "no-formats", "steps": steps,
                "headline": "YouTube refused to hand over this video's streams.",
                "explanation": "Invidious asked YouTube for playable formats and got none. YouTube rejects these requests when the attestation token cannot be validated, most often because the traffic leaves through a VPN, a datacenter, or a shared/flagged IP address.",
                "remedies": [
                    "If you are on a VPN, turn it off for this machine (or split-tunnel Docker) and try again.",
                    "Set COMPANION_PROXY to an egress YouTube trusts.",
                    "Set COMPANION_YOUTUBE_COOKIES to a signed-in YouTube session.",
                    _fallback_remedy("Meanwhile, the YouTube player under Settings > Privacy can play it, if you accept that Google sees your IP address and what you watch."),
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
                "remedies": ["Try another video. This one may be live, private, age-restricted, or region-locked."]}

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
                "explanation": "Stream URLs are signed for one IP address. A 403 usually means your connection changed between the URL being issued and the video being played. Toggling a VPN mid-session does exactly this.",
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


# Every hop _diagnose_playback can reach, in order. It stops at the first break, so anything
# past that was skipped rather than passed, and the display has to be able to say so.
PLAYBACK_HOPS = ("Invidious reachable", "Player page", "Stream manifest", "Media segment")


@app.get("/api/playback/diagnose")
def api_playback_diagnose(video_id: str = "") -> dict[str, Any]:
    return {"ok": True, "hops": list(PLAYBACK_HOPS), "diagnosis": _diagnose_playback(video_id.strip() or _playback_probe_video())}


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
        # Third-party players this user opted into. Never tried automatically.
        "on_request": _on_request_sources(user_key),
        "deployment_fallbacks": settings.invidious_fallback_list,
        "youtube_fallback_allowed": settings.youtube_fallback_enabled,
        "public_instances_allowed": privacy.public_instances_allowed(),
        "local_backend_degraded": playback_backend_degraded(),
    }


@app.post("/api/playback/sources/test")
def api_playback_test(request: Request, source_id: str = "") -> dict[str, Any]:
    """Probe every enabled source, or one, which is how Settings can report real progress."""
    user_key = _user_key(request)
    probe_video = _playback_probe_video()
    sources = _configured_playback_sources(user_key) + _on_request_sources(user_key)
    if source_id:
        sources = [source for source in sources if source["id"] == source_id]
        if not sources:
            raise HTTPException(status_code=404, detail="No enabled source with that id")
    results = [_probe_playback_source(source, probe_video) for source in sources]
    return {"ok": True, "video_id": probe_video, "results": results}


@app.post("/api/playback/report")
def api_playback_report(request: Request, payload: PlaybackReportRequest) -> dict[str, Any]:
    """The player tells us what really happened, which beats any probe.

    Source health is shared by everyone on the server: a failure puts a source in cooldown
    for all. On a shared install only an admin's player moves it, so a member cannot switch
    a source off for everyone by reporting failures."""
    if not policy.is_member(_current_user(request)):
        mark_source_result(payload.source_id, payload.ok)
    return {"ok": True, "health": source_health_snapshot(payload.source_id)}


@app.get("/api/playback/preferences")
def api_playback_preferences(request: Request) -> dict[str, Any]:
    return {"ok": True, "preferences": _json_ready_value(db.get_playback_preferences(_user_key(request)))}


@app.post("/api/playback/preferences")
def api_update_playback_preferences(request: Request, payload: PlaybackPreferencesRequest) -> dict[str, Any]:
    # Addresses already stored pass through, so a bad one saved before this check existed
    # can still be removed one chip at a time.
    stored = db.get_playback_preferences(_user_key(request)).get("public_invidious_urls") or []
    if payload.public_invidious_urls and any(url not in stored for url in payload.public_invidious_urls) \
            and not privacy.user_instances_allowed():
        raise HTTPException(status_code=400, detail="Adding public instances is turned off on this server.")
    for url in payload.public_invidious_urls or []:
        problem = "" if url in stored else db.instance_url_problem(url)
        if problem:
            raise HTTPException(status_code=400, detail=f"Not added: {url!r} {problem}.")
    preferences = db.set_playback_preferences(
        prefer_local=payload.prefer_local,
        public_invidious_enabled=payload.public_invidious_enabled,
        public_invidious_urls=payload.public_invidious_urls,
        youtube_fallback=payload.youtube_fallback,
        source_order=payload.source_order,
        disabled_sources=payload.disabled_sources,
        user_key=_user_key(request),
        captions_enabled=payload.captions_enabled,
        captions_language=payload.captions_language,
        captions_auto=payload.captions_auto,
        live_chat_enabled=payload.live_chat_enabled,
        autoplay=payload.autoplay,
        default_speed=payload.default_speed,
    )
    return {"ok": True, "preferences": _json_ready_value(preferences)}


class PrivacyChoicesRequest(BaseModel):
    youtube_player: bool | None = None
    public_instances: bool | None = None


def _privacy_payload(user_key: str) -> dict[str, Any]:
    services = _privacy_services(user_key)
    return {
        "ok": True,
        "choices": privacy.get_choices(user_key),
        "services": services,
        "summary": _privacy_summary(services),
        "outbound_proxy": bool(outbound.proxy_url()),
        "logs_client_details": privacy.log_client_details(),
    }


@app.get("/api/privacy", tags=["preferences"], summary="Which outside services are on for you, and what each one sees")
def api_privacy(request: Request) -> dict[str, Any]:
    return _privacy_payload(_user_key(request))


@app.post("/api/privacy", tags=["preferences"], summary="Opt in to or out of third-party players")
def api_update_privacy(request: Request, payload: PrivacyChoicesRequest) -> dict[str, Any]:
    """Turning a player on needs the admin to allow it first; turning one off always works."""
    user_key = _user_key(request)
    if payload.youtube_player and not privacy.youtube_allowed():
        raise HTTPException(status_code=403, detail="The YouTube player is turned off on this server.")
    if payload.public_instances and not privacy.public_instances_allowed():
        raise HTTPException(status_code=403, detail="Public instances are turned off on this server.")
    privacy.set_choices(user_key, youtube_player=payload.youtube_player, public_instances=payload.public_instances)
    return _privacy_payload(user_key)


@app.get("/docs", include_in_schema=False, response_class=HTMLResponse)
def api_reference() -> HTMLResponse:
    """The API reference, from this origin. See apidocs.py for why not Swagger UI."""
    from . import apidocs

    return HTMLResponse(apidocs.reference_html(app.openapi()))


@app.get("/redoc", include_in_schema=False)
def api_reference_redoc() -> RedirectResponse:
    return RedirectResponse(url="/docs", status_code=307)


@app.get("/api/recommendations/library")
def api_library_recommendations(request: Request, limit: int = 50) -> dict[str, Any]:
    return {"items": _json_ready(db.list_subscription_recommendations(max(1, min(limit, 200)), _user_key(request)))}


@app.get("/api/library")
def api_library(request: Request, limit: int = 80) -> dict[str, Any]:
    return {"items": _json_ready(db.list_library_activity(max(1, min(limit, 200)), _user_key(request)))}


@app.get("/api/channels")
def api_channels(request: Request, limit: int = 200) -> dict[str, Any]:
    """Stored channels. A member sees the channels they follow; the full list would name
    every channel anyone on this server follows."""
    capped = max(1, min(limit, 500))
    # On a shared install an admin too gets the channels they follow: the stored list also
    # holds every channel anybody searched for or opened (catalog_scope.py).
    if policy.is_member(_current_user(request)) or catalog_scope.applies(_user_key(request)):
        user_key = _user_key(request)
        followed = {str(row.get("youtube_channel_id") or "") for row in db.list_subscriptions(2000, user_key=user_key)}
        return {"items": _json_ready([row for row in db.list_channels(5000) if str(row.get("youtube_channel_id") or "") in followed][:capped])}
    return {"items": _json_ready(db.list_channels(capped))}


@app.get("/api/channels/{youtube_channel_id}")
def api_channel(request: Request, youtube_channel_id: str) -> dict[str, Any]:
    """One channel; subscription_id/subscription_title are the caller's own subscription."""
    channel = _json_ready_one(db.get_channel_by_youtube_id(youtube_channel_id, _user_key(request)))
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
        feed_title = ""
        for entry in parse_channel_feed(result.text, feed_url):
            feed_title = feed_title or str(entry.get("channel_title") or "")
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
            if feed_title:
                db.refresh_feed_title(subscription_id, feed_title)
        return result.status
    except Exception as exc:  # noqa: BLE001 - the subscribe already succeeded
        logger.info("single-feed fetch skipped for %s: %s", feed_url, exc)
        return None


def _subscribe_channel_local(
    youtube_channel_id: str = "", title: str = "", feed_url: str = "", *, user_key: str
) -> dict[str, Any]:
    """Subscribe one user to a channel's feed. No remote service involved.

    The channel id is resolved from any of the inputs (a bare channel id, a feed
    URL carrying ``?channel_id=``, or a title mentioning one) and the channel
    RSS feed URL is derived from it. A bare id that is not a well-formed channel
    id is rejected before it can mint a junk row; a bare feed URL (OPML, Takeout,
    non-YouTube feeds) is always accepted and fetched best-effort. The shared
    feeds row is upserted under a ``feed/<url>`` id (the dedupe pass collapses it
    into an existing row for the same feed) and the user gets a user_subscriptions
    row for it. Then that one feed is pulled immediately on a best-effort basis; a
    fetch failure never fails the subscribe. Returns the stored feed_url,
    youtube_channel_id, title, feed_id and the immediate fetch's HTTP status (None
    when the fetch was skipped or never answered).
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
    stored = db.subscribe(
        user_key,
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
        db.upsert_channel(name, resolved_id, url, keep_title=True)
        feed_status = _fetch_single_feed_best_effort(url, resolved_id)
        # One channel, off-thread: the subscribe response should not wait on artwork.
        threading.Thread(target=lambda: enrich_channel(resolved_id), name="channel-art", daemon=True).start()
    return {"youtube_channel_id": resolved_id or None, "feed_url": url, "title": name,
            "feed_id": stored["feed_id"], "feed_status": feed_status}


@app.post("/api/subscriptions", tags=["subscriptions"], summary="Subscribe to a channel or feed")
def api_subscribe(request: Request, payload: SubscribeRequest) -> dict[str, Any]:
    """Subscribe the caller (this profile) to a YouTube channel id or a feed URL.

    The feed itself is shared: a channel several people follow is fetched once. Returns
    `{ok, feed_url, youtube_channel_id, title}`; subscribing twice is not an error.
    """
    user_key = _user_key(request)
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
        stored = _subscribe_channel_local(youtube_channel_id, title, feed_url, user_key=user_key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "feed_url": stored["feed_url"], "youtube_channel_id": stored["youtube_channel_id"], "title": stored["title"]}


@app.delete("/api/subscriptions", tags=["subscriptions"], summary="Unsubscribe from a channel or feed")
def api_unsubscribe(request: Request, payload: SubscribeRequest) -> dict[str, Any]:
    """Unsubscribe the caller (this profile) only. Other people keep following it, and the
    feed's videos and everyone's history stay. 404 when the caller did not follow it."""
    user_key = _user_key(request)
    youtube_channel_id = payload.youtube_channel_id.strip()
    feed_url = payload.feed_url.strip() or (_youtube_feed_url(youtube_channel_id) if youtube_channel_id else "")
    if not feed_url:
        raise HTTPException(status_code=400, detail="A feed URL or YouTube channel ID is required")

    # This user's follow only: the feed, its videos and watch history stay, so
    # unsubscribing never deletes anything anybody has already seen.
    removed = db.remove_subscription(feed_url, youtube_channel_id or None, user_key=user_key)
    if not removed:
        raise HTTPException(status_code=404, detail="No such subscription")
    return {"ok": True, "feed_url": feed_url, "youtube_channel_id": youtube_channel_id or None, "removed_local": removed}


@app.get("/api/subscriptions", tags=["subscriptions"], summary="List your subscriptions")
def api_subscriptions(request: Request, limit: int = 500) -> dict[str, Any]:
    """The caller's (this profile's) subscriptions, by title. Each item: id, title, feed_url,
    html_url, youtube_channel_id, updated_at, channel_avatar_url, video_count,
    latest_video_at."""
    return {"items": _json_ready(db.list_subscriptions(max(1, min(limit, 1000)), user_key=_user_key(request)))}


def _require_collection_id(collection_id: str) -> None:
    """A malformed id (for example "None" from a stale client) is a missing collection, not a 500."""
    if not _valid_uuid(collection_id):
        raise HTTPException(status_code=404, detail="Collection not found")


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
    # Kept as a snapshot for a day so the toast's Undo can restore it (library_tools).
    undo_id = library_tools.delete_collection(collection_id, _user_key(request))
    if not undo_id:
        raise HTTPException(status_code=404, detail="Collection not found")
    return {"ok": True, "undo_id": undo_id}


@app.get("/api/subscription-collections/{collection_id}/subscriptions")
def api_collection_subscriptions(request: Request, collection_id: str) -> dict[str, Any]:
    _require_collection_id(collection_id)
    return {"items": _json_ready_value(db.list_collection_subscriptions(collection_id, _user_key(request)))}


@app.get("/api/subscription-collections/{collection_id}/videos")
def api_collection_videos(request: Request, collection_id: str, limit: int = 80) -> dict[str, Any]:
    _require_collection_id(collection_id)
    return {"items": _json_ready_value(db.list_collection_videos(collection_id, max(1, min(limit, 200)), _user_key(request)))}


@app.post("/api/subscription-collections/{collection_id}/members")
def api_add_subscription_collection_member(request: Request, collection_id: str, payload: SubscriptionCollectionMemberRequest) -> dict[str, Any]:
    _require_collection_id(collection_id)
    item = db.add_subscription_to_collection(collection_id, payload.subscription_id.strip(), _user_key(request))
    if not item:
        raise HTTPException(status_code=404, detail="Collection or subscription not found")
    return {"ok": True, "item": _json_ready_value(item)}


@app.delete("/api/subscription-collections/{collection_id}/members", tags=["subscriptions"], summary="Remove a collection member")
def api_remove_subscription_collection_member_body(
    request: Request, collection_id: str, payload: SubscriptionCollectionMemberRequest,
) -> dict[str, Any]:
    """Remove a member of the caller's collection using its exact feed identifier.

    Send JSON `{"subscription_id": "feed/4"}`; no URL encoding is needed. Returns
    `{"ok": true}`, or 404 when the member is absent or the collection belongs to
    another user. The path-based endpoint remains available as a compatibility alias.
    """
    return api_remove_subscription_collection_member(request, collection_id, payload.subscription_id)


@app.delete("/api/subscription-collections/{collection_id}/members/{subscription_id:path}")
def api_remove_subscription_collection_member(request: Request, collection_id: str, subscription_id: str) -> dict[str, Any]:
    # Local subscription ids contain a slash ("feed/4"), so this segment must be a path param.
    _require_collection_id(collection_id)
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
    if event_type == "watch_page_open" and library_tools.history_paused(user_key):
        # Watch history is paused: opening a video (the Shorts viewer reports it here) leaves no trace.
        return {"ok": True, "paused": True, "event": None, "feedback": {}}
    # What an Undo puts back (POST /api/feedback/undo), read before the press changes it.
    undoable = event_type in search_extras.UNDOABLE_EVENTS
    previous = search_extras.feedback_snapshot(db.get_video_feedback(youtube_video_id, user_key)) if undoable else None
    event = db.record_video_event(youtube_video_id, event_type, feedback.payload, user_key)
    if not event:
        raise HTTPException(status_code=404, detail="Video not found")
    state = db.set_video_feedback(youtube_video_id, event_type, feedback.payload, user_key)
    if event_type == "watch_page_open":
        db.update_playback_progress(youtube_video_id, 0, None, "open", feedback.payload, user_key)
    if not state:
        state = db.get_video_feedback(youtube_video_id, user_key) or {}
    result = {"ok": True, "event": _json_ready_value(event), "feedback": _json_ready_value(state)}
    if undoable:
        result["previous"] = previous
    return result


@app.post("/api/playback/progress")
def api_playback_progress(request: Request, progress: PlaybackProgressRequest) -> dict[str, Any]:
    user_key = _user_key(request)
    event_type = progress.event_type.strip() or "progress"
    youtube_video_id = progress.youtube_video_id.strip()
    if library_tools.history_paused(user_key):
        # Paused watch history keeps no resume point and adds no watch time, as on YouTube.
        return {"ok": True, "paused": True, "progress": None}
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
    user_key = _user_key(request)
    playlist = db.create_playlist(name, payload.description, user_key)
    visibility = library_tools.clean_visibility(payload.visibility)
    if visibility != "private" and playlist.get("id"):
        playlist = library_tools.update_playlist_settings(str(playlist["id"]), user_key, visibility=visibility) or playlist
    return {"ok": True, "playlist": _json_ready_value(playlist)}


@app.get("/api/playlists")
def api_playlists(request: Request, video: str = "") -> dict[str, Any]:
    """The caller's playlists. ?video=<id> also fills has_video, for the save picker."""
    video_id = video.strip()
    user_key = _user_key(request)
    rows = db.list_playlists(user_key, video_id) if video_id else db.list_playlists(user_key)
    items = [
        {
            **row,
            "cover_url": _thumbnail_for({
                "youtube_video_id": row.get("cover_youtube_video_id"),
                "thumbnail_url": row.get("cover_thumbnail_url"),
            }),
        }
        for row in rows
    ]
    return {"items": _json_ready_value(items)}


@app.patch("/api/playlists/{playlist_id}")
def api_update_playlist(request: Request, playlist_id: str, payload: PlaylistRequest) -> dict[str, Any]:
    if not _valid_uuid(playlist_id):
        raise HTTPException(status_code=404, detail="Playlist not found")
    playlist = db.update_playlist(playlist_id, payload.name.strip(), payload.description, _user_key(request))
    if not playlist:
        raise HTTPException(status_code=404, detail="Playlist not found")
    if payload.visibility is not None:
        playlist = library_tools.update_playlist_settings(playlist_id, _user_key(request), visibility=payload.visibility) or playlist
    return {"ok": True, "playlist": _json_ready_value(playlist)}


@app.delete("/api/playlists/{playlist_id}")
def api_delete_playlist(request: Request, playlist_id: str) -> dict[str, Any]:
    if not _valid_uuid(playlist_id):
        raise HTTPException(status_code=404, detail="Playlist not found")
    # Kept as a snapshot for a day so the toast's Undo can restore it (library_tools).
    undo_id = library_tools.delete_playlist(playlist_id, _user_key(request))
    if not undo_id:
        raise HTTPException(status_code=404, detail="Playlist not found")
    return {"ok": True, "deleted": 1, "undo_id": undo_id}


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
    """A playlist and its videos: the caller's own, or one another profile or account made
    unlisted or public (the same ones /playlist/<id> shows), read-only. is_owner says which;
    someone else's comes with owner_name and without the owner's internal key."""
    user_key = _user_key(request)
    if not _valid_uuid(playlist_id):
        raise HTTPException(status_code=404, detail="Playlist not found")
    playlist = _viewable_playlist(playlist_id, user_key)
    if not playlist:
        raise HTTPException(status_code=404, detail="Playlist not found")
    owner = bool(playlist.get("is_owner"))
    owner_key = user_key if owner else str(playlist.get("user_key") or "")
    videos = db.list_playlist_videos(playlist_id, max(1, min(limit, 500)), owner_key)
    shown = dict(playlist)
    if not owner:
        shown.pop("user_key", None)
    shown["is_owner"] = owner
    shown["read_only"] = not owner
    return {"playlist": _json_ready_one(shown), "items": _json_ready(videos)}


@app.delete("/api/playlists/{playlist_id}/items/{youtube_video_id}")
def api_remove_playlist_item(request: Request, playlist_id: str, youtube_video_id: str) -> dict[str, Any]:
    if not _valid_uuid(playlist_id):
        raise HTTPException(status_code=404, detail="Playlist not found")
    removed = db.remove_video_from_playlist(playlist_id, youtube_video_id, _user_key(request))
    if not removed:
        raise HTTPException(status_code=404, detail="Playlist item not found")
    return {"ok": True, "removed": removed}


@app.delete("/api/history/{youtube_video_id}")
def api_remove_history_video(request: Request, youtube_video_id: str) -> dict[str, Any]:
    """Remove one video from the caller's watch history, resume position included."""
    auth_redirect = _login_required_redirect(request)
    if auth_redirect:
        raise HTTPException(status_code=401, detail="Login required")
    removed = db.remove_history_video(youtube_video_id, _user_key(request))
    if not removed:
        raise HTTPException(status_code=404, detail="Not in history")
    return {"ok": True, "removed": removed}


# ---- Library tools API: history pause and clear, bulk edits, playlist order, sort,
# visibility, YouTube import and undo for deletes. The SQL is in library_tools.py. ----


class HistoryPauseRequest(BaseModel):
    paused: bool


class VideoIdsRequest(BaseModel):
    youtube_video_ids: list[str] = Field(default_factory=list, max_length=500)


class PlaylistMoveRequest(BaseModel):
    youtube_video_id: str
    # top, bottom, up or down; or index, the 0-based place it should end up at.
    move: str = ""
    index: int | None = None


class PlaylistSettingsRequest(BaseModel):
    visibility: str | None = None
    sort: str | None = None


class PlaylistImportRequest(BaseModel):
    url: str
    name: str = ""
    visibility: str = "private"


def _library_user(request: Request) -> str:
    """The caller's user_key, for a library write. 401 when sign-in is required and missing."""
    if _login_required_redirect(request):
        raise HTTPException(status_code=401, detail="Login required")
    return _user_key(request)


def _owned_playlist_id(playlist_id: str) -> str:
    if not _valid_uuid(playlist_id):
        raise HTTPException(status_code=404, detail="Playlist not found")
    return playlist_id


@app.get("/api/history/settings", tags=["library"], summary="Whether watch history is paused")
def api_history_settings(request: Request) -> dict[str, Any]:
    return {"ok": True, "paused": library_tools.history_paused(_library_user(request))}


@app.post("/api/history/pause", tags=["library"], summary="Pause or turn on watch history")
def api_history_pause(request: Request, payload: HistoryPauseRequest) -> dict[str, Any]:
    """While paused, watching adds nothing to history, resume points or recommendations."""
    return {"ok": True, "paused": library_tools.set_history_paused(_library_user(request), payload.paused)}


@app.post("/api/history/clear", tags=["library"], summary="Clear all watch history")
def api_history_clear(request: Request) -> dict[str, Any]:
    return {"ok": True, "removed": library_tools.clear_history(_library_user(request))}


@app.post("/api/history/remove", tags=["library"], summary="Remove several videos from watch history")
def api_history_remove_many(request: Request, payload: VideoIdsRequest) -> dict[str, Any]:
    return {"ok": True, "removed": library_tools.remove_history_videos(payload.youtube_video_ids, _library_user(request))}


@app.post("/api/liked/remove", tags=["library"], summary="Remove videos from Liked videos")
def api_liked_remove(request: Request, payload: VideoIdsRequest) -> dict[str, Any]:
    return {"ok": True, "removed": library_tools.remove_liked_videos(payload.youtube_video_ids, _library_user(request))}


@app.post("/api/playlists/{playlist_id}/items/bulk", tags=["library"], summary="Add several videos to a playlist")
def api_playlist_add_many(request: Request, playlist_id: str, payload: VideoIdsRequest) -> dict[str, Any]:
    user_key = _library_user(request)
    _owned_playlist_id(playlist_id)
    if not db.get_playlist(playlist_id, user_key):
        raise HTTPException(status_code=404, detail="Playlist not found")
    return {"ok": True, "added": library_tools.add_videos_to_playlist(playlist_id, payload.youtube_video_ids, user_key)}


@app.post("/api/playlists/{playlist_id}/items/remove", tags=["library"], summary="Remove several videos from a playlist")
def api_playlist_remove_many(request: Request, playlist_id: str, payload: VideoIdsRequest) -> dict[str, Any]:
    user_key = _library_user(request)
    _owned_playlist_id(playlist_id)
    # Someone else's playlist (even a public one) is "not found", like the other writes.
    if not db.get_playlist(playlist_id, user_key):
        raise HTTPException(status_code=404, detail="Playlist not found")
    removed = library_tools.remove_playlist_items(playlist_id, payload.youtube_video_ids, user_key)
    return {"ok": True, "removed": removed}


@app.post("/api/playlists/{playlist_id}/items/move", tags=["library"], summary="Move a video within a playlist")
def api_playlist_move(request: Request, playlist_id: str, payload: PlaylistMoveRequest) -> dict[str, Any]:
    """Reorder by one video: Move to top/bottom, up/down, or a drop at an index.

    This edits the manual order, which is what the page shows while Sort is Manual."""
    user_key = _library_user(request)
    _owned_playlist_id(playlist_id)
    if payload.index is None and payload.move not in library_tools.MOVES:
        raise HTTPException(status_code=400, detail="Unknown move")
    order = library_tools.move_playlist_item(playlist_id, payload.youtube_video_id.strip(), user_key, payload.move, payload.index)
    if order is None:
        raise HTTPException(status_code=404, detail="Playlist item not found")
    return {"ok": True, "order": order}


@app.post("/api/playlists/{playlist_id}/settings", tags=["library"], summary="Set a playlist's visibility or sort")
def api_playlist_settings(request: Request, playlist_id: str, payload: PlaylistSettingsRequest) -> dict[str, Any]:
    user_key = _library_user(request)
    playlist = library_tools.update_playlist_settings(
        _owned_playlist_id(playlist_id), user_key, visibility=payload.visibility, sort_key=payload.sort,
    )
    if not playlist:
        raise HTTPException(status_code=404, detail="Playlist not found")
    return {"ok": True, "playlist": _json_ready_value(playlist)}


@app.post("/api/playlists/import", tags=["library"], summary="Import a YouTube playlist by link")
def api_playlist_import(request: Request, payload: PlaylistImportRequest) -> dict[str, Any]:
    """Starts a background import and returns a job to poll at /api/playlists/import/{id}."""
    user_key = _library_user(request)
    try:
        job = library_tools.start_import(payload.url, user_key, payload.name, payload.visibility)
    except library_tools.ImportRefused as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "job": job}


@app.get("/api/playlists/import/{job_id}", tags=["library"], summary="Progress of a playlist import")
def api_playlist_import_job(request: Request, job_id: str) -> dict[str, Any]:
    job = library_tools.get_import(job_id, _library_user(request))
    if not job:
        raise HTTPException(status_code=404, detail="No such import")
    return {"ok": True, "job": job}


@app.post("/api/library/restore/{undo_id}", tags=["library"], summary="Undo deleting a playlist or collection")
def api_library_restore(request: Request, undo_id: str) -> dict[str, Any]:
    user_key = _library_user(request)
    if not _valid_uuid(undo_id):
        raise HTTPException(status_code=404, detail="Nothing to restore")
    restored = library_tools.restore_deleted(undo_id, user_key)
    if not restored:
        raise HTTPException(status_code=409, detail="It could not be restored")
    return {"ok": True, **restored}


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
    # An unknown host is dropped rather than handed to the browser.
    return privacy.same_origin_image(text)


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


def _may_download(request: Request | None) -> bool:
    """Members may download unless the admin turned it off (Admin, Server, Downloads)."""
    try:
        return not policy.is_member(_current_user(request)) or instance.members_may_download()
    except Exception:  # noqa: BLE001 - the button shows; the API still decides
        return True


def _download_quota(request: Request) -> int:
    """This caller's storage quota in bytes, 0 for none. Only members have one."""
    return instance.download_quota_bytes() if policy.is_member(_current_user(request)) else 0


def _require_download_rights(request: Request, destination: str = "local") -> int:
    """Refuse a download a member may not start. Returns the quota for a kept download."""
    if not _may_download(request):
        raise HTTPException(status_code=403, detail="Downloading is turned off for members on this server.")
    quota = _download_quota(request) if destination == "server" else 0
    if quota:
        used = downloads.account_usage_bytes(policy.account_key(_current_user(request)))
        if used >= quota:
            raise HTTPException(
                status_code=403,
                detail=f"Your downloads use {_bytes_text(used)} of your {_bytes_text(quota)} on this server. "
                       "Delete some under Downloads to keep more.",
            )
    return quota


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
    _require_download_rights(request)
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
    try:
        netguard.assert_public_url(stream_url)
    except netguard.UnsafeExternalUrl as exc:
        logger.warning("download stream for %s pointed at an unsafe host: %s", youtube_video_id, exc)
        raise HTTPException(status_code=502, detail="The stream host is not a public media endpoint") from exc
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


@app.get("/api/download/{youtube_video_id}/info")
def api_download_info(request: Request, youtube_video_id: str) -> dict[str, Any]:
    """What the download button would give you, so the UI can say so before starting."""
    auth_redirect = _login_required_redirect(request)
    if auth_redirect:
        raise HTTPException(status_code=401, detail="Login required")
    _require_download_rights(request)
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
    _require_download_rights(request)

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
    try:
        free_bytes = downloads.disk_usage().get("free_bytes")
    except OSError:
        free_bytes = None
    quota = _download_quota(request)
    if quota:
        # A member sees their own room, never the server's disk.
        free_bytes = max(0, quota - downloads.account_usage_bytes(policy.account_key(_current_user(request))))
    return {
        "ok": bool(formats),
        "title": title or youtube_video_id,
        "formats": formats,
        "free_bytes": free_bytes,
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
    quota = _require_download_rights(request, payload.destination)
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
            quota_bytes=quota,
            quota_account=policy.account_key(_current_user(request)),
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
    # The stored path is a server detail; the page uses it, the API does not expose it.
    rows = [{key: value for key, value in row.items() if key != "path"} for row in db.list_downloads(_user_key(request), q, limit)]
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


def _api_profile_summary(user: dict[str, Any]) -> dict[str, Any] | None:
    """Which profile this credential acts as. A token carries only its user_key."""
    if user.get("via_token"):
        try:
            row = profiles.profile_for_user_key(str(user.get("user_key") or ""))
        except Exception:  # noqa: BLE001 - informational only
            row = None
    else:
        row = user.get("profile")
    if not row:
        return None
    return {"id": str(row.get("id") or ""), "name": row.get("name"), "is_kids": bool(row.get("is_kids")), "is_main": bool(row.get("is_main"))}


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
        "profile": _api_profile_summary(user),
        "documentation": {"swagger": "/docs", "redoc": "/redoc", "openapi": "/openapi.json"},
        "capabilities": {
            "feed": ["/api/home/sections", "/api/recommendations", "/api/shorts", "/api/search"],
            "videos": ["/api/videos", "/api/videos/{id}", "/api/transcript/{id}", "/api/transcript/{id}/fetch", "/api/feedback"],
            "playback": ["/api/playback/sources", "/api/playback/progress/{id}", "/api/playback/diagnose"],
            "downloads": ["/api/download/{id}/formats", "/api/download/{id}/jobs", "/api/downloads"],
            "subscriptions": ["/api/subscriptions", "/api/channels", "/api/subscription-collections"],
            "playlists": ["/api/playlists"],
            "preferences": ["/api/feed/preferences", "/api/playback/preferences"],
            "metadata": ["/api/enrich/recent", "/api/discover/related", "/api/ryd/refresh"],
            "tokens": ["/api/tokens"],
            "profiles": ["/api/profiles"],
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
    # Read capped: `await archive.read()` with no limit would pull the entire body into
    # one bytes object before any check — a 10 GiB upload would OOM the server before
    # takeout.inspect ever got the chance to reject it. Also refuse early on an
    # obviously oversized Content-Length so the bytes never leave the socket buffer.
    # The admin can set a smaller ceiling than the parser's own (Admin, Server, Limits).
    max_bytes = min(takeout.MAX_ARCHIVE_BYTES, instance.takeout_max_bytes())
    too_large = f"That archive is larger than this server accepts ({_bytes_text(max_bytes)}). Export only YouTube history and subscriptions."
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > max_bytes:
        raise HTTPException(status_code=413, detail=too_large)
    buffer = bytearray()
    while True:
        chunk = await archive.read(1024 * 1024)
        if not chunk:
            break
        buffer.extend(chunk)
        if len(buffer) > max_bytes:
            raise HTTPException(status_code=413, detail=too_large)
    data = bytes(buffer)
    del buffer
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


@app.get("/api/export", tags=["metadata"], summary="Download your data as one JSON file")
def api_export(request: Request) -> Response:
    """Settings, Account, Your data. Only the requesting profile's rows (dataexport.py)."""
    user = _current_user(request)
    user_key = _user_key(request)
    name = str(_active_profile(user).get("name") or user.get("display_name") or "") if not user.get("via_token") else ""
    data = dataexport.build_export(user_key, profile_name=name)
    return Response(
        dataexport.to_json(data), media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{dataexport.export_filename()}"',
                 "Cache-Control": "private, no-store"},
    )


@app.get("/api/export/subscriptions.opml", tags=["metadata"], summary="Download your subscriptions as OPML")
def api_export_opml(request: Request) -> Response:
    """Your (this profile's) subscription list in the format feed readers import."""
    return Response(
        dataexport.subscriptions_opml(dataexport.subscription_channels(_user_key(request))), media_type="text/x-opml; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{dataexport.export_filename(suffix="opml")}"',
                 "Cache-Control": "private, no-store"},
    )


def _fetch_new_feeds(feeds: list[tuple[str, str | None]]) -> None:
    """Pull each newly imported feed once, one after another, so an import shows videos
    before the worker's next pass. Best effort: the poller covers anything missed."""
    for url, channel_id in feeds:
        _fetch_single_feed_best_effort(url, channel_id)
    if any(channel_id for _, channel_id in feeds):
        enrich_channels_paced(len(feeds))


@app.post("/api/import/subscriptions.opml", tags=["subscriptions"], summary="Import subscriptions from OPML")
async def api_import_opml(request: Request, file: UploadFile = File(...)) -> dict[str, Any]:
    """Subscribe the caller (this profile only) to every feed in an OPML file.

    The format every feed reader exports, and what Settings' OPML download produces. At
    most 500 feeds and 2 MB; YouTube channel feeds are stored by channel. Returns
    `{ok, total, subscribed, already}`; the new feeds are fetched in the background.
    """
    user_key = _user_key(request)
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > opml.MAX_BYTES + 65536:
        raise HTTPException(status_code=413, detail="That file is larger than an OPML list should be (2 MB).")
    data = await file.read(opml.MAX_BYTES + 1)
    try:
        feeds = opml.parse(data)
    except opml.OpmlError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    created: list[tuple[str, str | None]] = []
    already = 0
    for feed in feeds:
        channel_id = feed.get("youtube_channel_id")
        url = _youtube_feed_url(channel_id) if channel_id else str(feed["feed_url"])
        title = str(feed.get("title") or channel_id or url)
        stored = db.subscribe(user_key, {
            "id": f"feed/{url}", "title": title, "url": url,
            "htmlUrl": f"https://www.youtube.com/channel/{channel_id}" if channel_id else (feed.get("html_url") or None),
            "categories": [],
        }, channel_id)
        if channel_id:
            db.upsert_channel(title, channel_id, url, keep_title=True)
        if stored["created"]:
            created.append((url, channel_id))
        else:
            already += 1
    if created:
        threading.Thread(target=_fetch_new_feeds, args=(created,), name="opml-import", daemon=True).start()
    return {"ok": True, "total": len(feeds), "subscribed": len(created), "already": already}


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


# ---- Household profiles ("Who's watching") -------------------------------------------
# Data model and deletion live in profiles.py. Resolution happens in _with_profile, so
# every route that calls _user_key or _current_user already acts as the chosen profile.


def _profile_avatar(profile: dict[str, Any], size: str = "") -> str:
    """A profile's initial on its swatch. The colour comes from a closed list."""
    name = str(profile.get("name") or "Main")
    color = str(profile.get("color") or profiles.DEFAULT_COLOR)
    if color not in profiles.COLORS:
        color = profiles.DEFAULT_COLOR
    size_class = f" {size}" if size else ""
    return f'<span class="profile-avatar{size_class}" data-profile-color="{color}" aria-hidden="true">{escape(name[:1].upper() or "M")}</span>'


def _profile_select_form(profile: dict[str, Any], body: str, *, next_path: str = "/", css: str = "", attrs: str = "") -> str:
    """A plain POST form, so switching works without JavaScript and needs no token."""
    return (
        f'<form class="profile-select-form" method="post" action="/profiles/select">'
        f'<input type="hidden" name="next" value="{escape(_safe_next_path(next_path), quote=True)}">'
        f'<button class="{css}" type="submit" name="profile_id" value="{escape(str(profile.get("id") or "main"), quote=True)}"{attrs}>{body}</button></form>'
    )


def _profile_menu_block(user: dict[str, Any]) -> str:
    """Account-menu rows: the other profiles to switch to, then Manage or Add."""
    if user.get("via_token"):
        return ""
    rows = list(user.get("profiles") or [])
    active = _active_profile(user)
    others = [row for row in rows if row.get("id") != active.get("id")]
    switch = "".join(
        _profile_select_form(
            row,
            f'<span class="menu-icon">{_profile_avatar(row, "sm")}</span><span class="menu-copy"><span class="menu-label">{escape(str(row.get("name") or ""))}</span></span>'
            f'<span class="menu-trailing">{"Kids" if row.get("is_kids") else ""}</span>',
            css="menu-row", attrs=' role="menuitem"',
        )
        for row in others
    )
    if active.get("is_kids"):
        manage = ""
    else:
        label = "Manage profiles" if len(rows) > 1 else "Add a profile"
        manage = _menu_row("pencil" if len(rows) > 1 else "plus", label, attrs=' data-open-settings-dialog data-settings-target="profiles"')
    if not switch and not manage:
        return ""
    heading = '<p class="menu-group-label">Switch profile</p>' if switch else ""
    return f'<div class="menu-separator" role="separator"></div>{heading}<div class="profile-switch-list">{switch}</div>{manage}'


def _profile_tile(row: dict[str, Any], next_path: str) -> str:
    badge = '<span class="profile-tile-badge">Kids</span>' if row.get("is_kids") else ""
    body = f'{_profile_avatar(row, "xl")}<span class="profile-tile-name">{escape(str(row.get("name") or ""))}</span>{badge}'
    return f'<li>{_profile_select_form(row, body, next_path=next_path, css="profile-tile")}</li>'


def _profile_picker_html(user: dict[str, Any], next_path: str) -> str:
    tiles = "".join(_profile_tile(row, next_path) for row in _profile_list(user))
    account = str(user.get("email") or user.get("display_name") or "") if user.get("authenticated") else ""
    account_html = f'<p class="profile-picker-account">Signed in as {escape(account)}</p>' if account else ""
    return f'''<!doctype html>
    <html lang="en">
      <head><meta charset="utf-8"><title>Who's watching? - OpenFeed</title>{_shell_head()}</head>
      <body class="profile-picker-body">
        <main class="profile-picker" id="main">
          <a class="brand profile-picker-brand" href="/profiles" aria-label="OpenFeed">{_brand_mark()}<span class="brand-word">Open<b>Feed</b></span></a>
          <h1>Who&rsquo;s watching?</h1>
          <ul class="profile-picker-grid" role="list">{tiles}</ul>
          <p class="profile-picker-note">Each profile keeps its own subscriptions, history, likes, playlists and recommendations.</p>
          {account_html}
        </main>
      </body>
    </html>'''


_PROFILE_COLOR_LABELS = {"graphite": "Graphite", "slate": "Slate", "sage": "Sage", "clay": "Clay", "plum": "Plum", "steel": "Steel"}

# Said once, here and in README: exactly what the kids flag changes.
PROFILE_KIDS_NOTE = (
    "A kids profile hides comments and Shorts everywhere, whatever its own switches say, closes the Shorts "
    "viewer, and will not open a video YouTube marks as age-restricted. YouTube's “made for kids” flag is "
    "not available through Invidious, so videos are not filtered by audience. Kids profiles cannot add, change "
    "or delete profiles. It is not a lock: anyone in the household can switch profiles."
)


def _profile_color_picker(selected: str, field_id: str) -> str:
    swatches = "".join(
        f'<label class="profile-swatch" title="{label}"><input class="sr-only" type="radio" name="color" value="{key}"'
        f'{" checked" if key == selected else ""} aria-label="{label}">'
        f'<span class="profile-avatar sm" data-profile-color="{key}" aria-hidden="true"></span></label>'
        for key, label in _PROFILE_COLOR_LABELS.items()
    )
    return f'<div class="profile-swatches" role="radiogroup" aria-label="Colour" id="{field_id}">{swatches}</div>'


def _profile_kids_toggle(checked: bool) -> str:
    """_toggle's markup, minus the .switch class that marks a preference switch.

    The settings script binds preference switches by class; this one is part of a form
    and is saved with it. .toggle-row styles it identically.
    """
    return (
        '<label class="toggle-row"><span><strong>Kids profile</strong><small>Hides comments and Shorts. See below for exactly '
        f'what it does.</small></span><input type="checkbox" role="switch" name="is_kids"{" checked" if checked else ""}></label>'
    )


def _profile_edit_form(row: dict[str, Any]) -> str:
    pid = escape(str(row.get("id") or "main"), quote=True)
    kids = "" if row.get("is_main") else _profile_kids_toggle(bool(row.get("is_kids")))
    return (
        f'<form class="profile-edit" data-profile-edit="{pid}" hidden>'
        f'<label class="text-field"><span class="set-label">Name</span><input class="field-input" name="name" required maxlength="{profiles.NAME_MAX}" '
        f'autocomplete="off" value="{escape(str(row.get("name") or ""), quote=True)}"></label>'
        f'<div class="text-field"><span class="set-label">Colour</span>{_profile_color_picker(str(row.get("color") or ""), f"profile-color-{pid}")}</div>'
        f'{kids}'
        '<div class="profile-edit-actions"><button class="btn" type="button" data-profile-cancel>Cancel</button>'
        '<button class="btn btn-primary" type="submit">Save</button></div></form>'
    )


def _profiles_pane(user: dict[str, Any]) -> str:
    if user.get("via_token"):
        return _empty_state("user", "Profiles need a browser session", "An API token acts as one profile and cannot switch or manage them.")
    rows = _profile_list(user)
    active = _active_profile(user)
    active_id = str(active.get("id") or "main")
    can_manage = not active.get("is_kids")
    items = []
    for row in rows:
        pid = str(row.get("id") or "main")
        is_active = pid == active_id
        meta = [part for part in ("Main profile" if row.get("is_main") else "", "Kids" if row.get("is_kids") else "", "Watching now" if is_active else "") if part]
        name = str(row.get("name") or "")
        actions = []
        if not is_active:
            actions.append(_profile_select_form(row, "Switch", css="btn btn-sm", attrs=f' aria-label="Switch to {escape(name, quote=True)}"'))
        if can_manage:
            actions.append(f'<button class="btn btn-sm" type="button" data-profile-edit-open="{escape(pid, quote=True)}" aria-label="Edit {escape(name, quote=True)}">Edit</button>')
            if not row.get("is_main"):
                actions.append(
                    f'<button class="btn btn-sm btn-danger" type="button" data-profile-delete="{escape(pid, quote=True)}" '
                    f'data-profile-name="{escape(name, quote=True)}" aria-label="Delete {escape(name, quote=True)}">Delete</button>'
                )
        items.append(
            f'<div class="profile-row" data-profile-row="{escape(pid, quote=True)}">'
            f'<div class="set-row">{_profile_avatar(row, "md")}<div class="set-copy"><strong>{escape(name)}</strong>'
            f'<small>{escape(" · ".join(meta)) or "&nbsp;"}</small></div>'
            f'<div class="set-control profile-actions">{"".join(actions)}</div></div>'
            f'{_profile_edit_form(row) if can_manage else ""}</div>'
        )
    count = len(rows) if user.get("profiles") else 1
    if not can_manage:
        add = _set_group("Managing profiles", _set_row("Switch to an adult profile", "Kids profiles can switch profiles but not add, change or delete them."))
    elif count >= profiles.MAX_PROFILES:
        add = _set_group("Add a profile", _set_row(f"{profiles.MAX_PROFILES} of {profiles.MAX_PROFILES} profiles", "Delete one to add another."))
    else:
        add = _set_group("Add a profile", (
            '<form class="profile-add" data-profile-add>'
            f'<label class="text-field"><span class="set-label">Name</span><input class="field-input" name="name" required maxlength="{profiles.NAME_MAX}" autocomplete="off" placeholder="Name"></label>'
            f'<div class="text-field"><span class="set-label">Colour</span>{_profile_color_picker(profiles.COLORS[(count) % len(profiles.COLORS)], "profile-color-new")}</div>'
            + _profile_kids_toggle(False)
            + '<div class="profile-edit-actions"><button class="btn btn-primary" type="submit">Add profile</button></div></form>'
        ), f'<p class="set-foot">{count} of {profiles.MAX_PROFILES} profiles.</p>')
    shared = _set_group("Shared and separate", (
        _set_row("Separate for each profile", "Subscriptions, history, continue watching, likes and dislikes, playlists, collections, "
                 "feed and playback preferences (including source order), notification levels, downloads, API tokens, Takeout "
                 "imports and recommendations. A new profile starts with no subscriptions.")
        + _set_row("Shared by the household", "The video library and server settings. A channel two profiles follow is still "
                   "fetched once, but following or unfollowing it on one profile leaves the others alone.")
    ), f'<p class="set-foot">{escape(PROFILE_KIDS_NOTE)}</p>')
    return f"""
      <p class="settings-lede">Profiles give each person in the household their own subscriptions, history and recommendations under one sign-in.</p>
      {_set_group("Profiles", "".join(items))}
      {add}
      {shared}
    """


@app.get("/profiles", response_class=HTMLResponse, include_in_schema=False)
def profile_picker(request: Request, next: str = "/"):
    user = _current_user(request)
    settings = get_settings()
    if settings.require_login and not user.get("authenticated"):
        return _login_required_redirect(request)
    if user.get("authenticated") and not _user_has_required_access(user):
        return _access_denied_response(user)
    target = _safe_next_path(next)
    if target.startswith("/profiles"):
        target = "/"
    if len(user.get("profiles") or []) < 2:
        return RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)
    return HTMLResponse(_profile_picker_html(user, target), headers={"cache-control": "private, no-store"})


def _profile_session_user(request: Request) -> dict[str, Any]:
    """The caller, if it may switch profiles: a browser, signed in where sign-in is required."""
    _user_key(request)  # login and access-group checks, as every route
    user = _current_user(request)
    if user.get("via_token"):
        raise HTTPException(status_code=403, detail="session_required")
    return user


def _set_profile_cookie(response: Response, user: dict[str, Any], profile: dict[str, Any]) -> None:
    # The main profile's id is stored too: it marks the choice as made, so the picker does
    # not ask again. Only the implicit main profile (no rows yet) has no id to store.
    if not profile.get("id"):
        response.delete_cookie(PROFILE_COOKIE, path="/")
        return
    response.set_cookie(
        PROFILE_COOKIE, _profile_cookie_value(str(user.get("account_key") or ""), str(profile["id"])),
        max_age=PROFILE_COOKIE_SECONDS, httponly=True, secure=_cookie_secure(), samesite="lax", path="/",
    )


def _account_profile(user: dict[str, Any], profile_id: str) -> dict[str, Any] | None:
    """One of this account's own profiles, never anyone else's."""
    wanted = str(profile_id or "").strip()
    for row in _profile_list(user):
        if (wanted == "main" and row.get("is_main")) or (wanted and row.get("id") == wanted):
            return row
    return None


@app.post("/profiles/select", include_in_schema=False)
async def profile_select(request: Request):
    """Form target for the picker and the account menu. Answers with a redirect."""
    user = _profile_session_user(request)
    form = await request.form()
    target = _safe_next_path(str(form.get("next") or "/"))
    if target.startswith("/profiles"):
        target = "/"
    profile = _account_profile(user, str(form.get("profile_id") or ""))
    if not profile:
        # Unknown, deleted, or another account's profile: refuse, and ask again.
        return RedirectResponse(f"/profiles?next={urllib.parse.quote(target)}", status_code=status.HTTP_303_SEE_OTHER)
    response = RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)
    _set_profile_cookie(response, user, profile)
    return response


class ProfileCreateRequest(BaseModel):
    name: str = Field(..., max_length=200)
    color: str = profiles.DEFAULT_COLOR
    is_kids: bool = False


class ProfileUpdateRequest(BaseModel):
    name: str | None = Field(None, max_length=200)
    color: str | None = None
    is_kids: bool | None = None


def _profile_public(row: dict[str, Any], active_id: str) -> dict[str, Any]:
    return {
        "id": str(row.get("id") or "main"), "name": row.get("name"), "color": row.get("color"),
        "is_kids": bool(row.get("is_kids")), "is_main": bool(row.get("is_main")),
        "active": str(row.get("id") or "main") == active_id,
    }


def _profiles_payload(user: dict[str, Any]) -> dict[str, Any]:
    active = _active_profile(user)
    active_id = str(active.get("id") or "main")
    return {
        "ok": True,
        "items": [_profile_public(row, active_id) for row in _profile_list(user)],
        "active": active_id,
        "limit": profiles.MAX_PROFILES,
        "colors": list(profiles.COLORS),
        "can_manage": not active.get("is_kids") and not user.get("via_token"),
    }


def _profile_manager(request: Request) -> dict[str, Any]:
    """Who may add, rename and delete: a browser session on a non-kids profile."""
    user = _profile_session_user(request)
    if _active_profile(user).get("is_kids"):
        raise HTTPException(status_code=403, detail="kids_profile")
    return user


def _profile_error(exc: profiles.ProfileError) -> JSONResponse:
    return JSONResponse({"ok": False, "detail": exc.code, "message": exc.message}, status_code=exc.status_code)


@app.get("/api/profiles", tags=["preferences"], summary="List this account's profiles")
def api_list_profiles(request: Request) -> dict[str, Any]:
    _user_key(request)
    return _profiles_payload(_current_user(request))


@app.post("/api/profiles", tags=["preferences"], summary="Add a profile", status_code=201)
def api_create_profile(request: Request, payload: ProfileCreateRequest):
    user = _profile_manager(request)
    try:
        row = profiles.create_profile(str(user["account_key"]), payload.name, payload.color, payload.is_kids, _profile_main_name(user))
    except profiles.ProfileError as exc:
        return _profile_error(exc)
    _forget_account_profiles(request)
    after = _current_user(request)
    response = JSONResponse({"ok": True, "item": _profile_public(row, ""), **{k: v for k, v in _profiles_payload(after).items() if k != "ok"}}, status_code=201)
    if not user.get("profile_chosen"):
        # Whoever added the profile is still watching as the main profile. Pin that, so
        # the reload that follows does not stop at "Who's watching".
        main_row = next((item for item in after.get("profiles") or [] if item.get("is_main")), None)
        if main_row:
            _set_profile_cookie(response, after, main_row)
    return response


@app.patch("/api/profiles/{profile_id}", tags=["preferences"], summary="Rename, recolour or change a profile's kids flag")
def api_update_profile(request: Request, profile_id: str, payload: ProfileUpdateRequest):
    user = _profile_manager(request)
    try:
        row = profiles.update_profile(
            str(user["account_key"]), profile_id, name=payload.name, color=payload.color, is_kids=payload.is_kids,
            main_name=_profile_main_name(user),
        )
    except profiles.ProfileError as exc:
        return _profile_error(exc)
    if not row:
        raise HTTPException(status_code=404, detail="No such profile")
    _forget_account_profiles(request)
    return {"ok": True, "item": _profile_public(row, "")}


def _after_user_keys_deleted(keys: list[str], download_rows: list[dict[str, Any]]) -> None:
    """After an account's rows are gone (accounts.delete_account): unlink its download files
    that nothing else references and drop in-process caches keyed by its user_keys."""
    for record in download_rows:
        path = _download_file_path(record)
        if path:
            downloads.unlink_unreferenced_download(path)
    for key in keys:
        db.clear_takeout_cache(key)
        db.clear_watch_time_cache(key)
        try:
            from . import ranker

            with ranker._USER_CACHE_LOCK:
                ranker._USER_CACHE.pop(key, None)
                ranker._USER_LOCKS.pop(key, None)
        except Exception:  # noqa: BLE001 - cache eviction is best effort
            pass


@app.delete("/api/profiles/{profile_id}", tags=["preferences"], summary="Delete a profile and all of its data")
def api_delete_profile(request: Request, profile_id: str):
    """Deletes the profile's history, likes, playlists, preferences, impressions, tokens
    and downloads. The main profile is the account itself and cannot be deleted."""
    user = _profile_manager(request)
    try:
        result = profiles.delete_profile(str(user["account_key"]), profile_id)
    except profiles.ProfileError as exc:
        return _profile_error(exc)
    if not result:
        raise HTTPException(status_code=404, detail="No such profile")
    key = result["user_key"]
    for record in result.pop("downloads", []):
        path = _download_file_path(record)
        if path:
            downloads.unlink_unreferenced_download(path)
    # In-process caches keyed by the deleted key. The rows are gone, so a rebuilt entry
    # would be empty anyway; this only returns the memory.
    db.clear_takeout_cache(key)
    db.clear_watch_time_cache(key)
    try:
        from . import ranker

        with ranker._USER_CACHE_LOCK:
            ranker._USER_CACHE.pop(key, None)
            ranker._USER_LOCKS.pop(key, None)
    except Exception:  # noqa: BLE001 - cache eviction is best effort
        pass
    _forget_account_profiles(request)
    response = JSONResponse({"ok": True, "deleted": result["id"], "removed": result["removed"]})
    if user.get("profile_chosen") and _active_profile(user).get("id") == result["id"]:
        response.delete_cookie(PROFILE_COOKIE, path="/")
    return response


@app.get("/api/videos/{youtube_video_id}", tags=["videos"], summary="Get one video")
def api_get_video(request: Request, youtube_video_id: str) -> dict[str, Any]:
    """Everything known about a single video, including this user's state for it."""
    user_key = _user_key(request)
    video = db.get_video_by_youtube_id(youtube_video_id, user_key)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    return {
        "ok": True,
        "video": _json_ready_value(video),
        "feedback": _json_ready_value(db.get_video_feedback(youtube_video_id, user_key) or {}),
        "progress": _json_ready_value(db.get_playback_progress(youtube_video_id, user_key) or {}),
        # The stored path is a server detail, as in /api/downloads.
        "download": _json_ready_value({key: value for key, value in (db.get_download(youtube_video_id, user_key) or {}).items() if key != "path"}),
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


@app.get("/api/transcript/{youtube_video_id}/fetch", tags=["videos"], summary="Fetch public captions for one video")
async def api_fetch_transcript(request: Request, youtube_video_id: str, language: str = "en",
                               kind: str = "prefer_manual", list_only: bool = False) -> dict[str, Any]:
    """Read-only, bounded retrieval through this install's Invidious (transcripts.py).

    Inspect `ok`/`status`/`code`: a provider failure is a 200 with `ok: false`, not an HTTP
    error. No stored row is required and nothing is stored. A malformed id, language or kind
    is a 422 `invalid_caption_request`.
    """
    _user_key(request)
    from pydantic import ValidationError

    try:
        payload = transcripts.TranscriptRequest(youtube_video_id=youtube_video_id,
                                                language=language, kind=kind, list_only=list_only)
    except ValidationError:
        raise HTTPException(status_code=422, detail="invalid_caption_request") from None
    return await transcripts.fetch(payload)


def _media_api_guard(request: Request, youtube_video_id: str) -> str:
    """Shared front door for the caption and chat endpoints: signed in, and a real video id."""
    from .youtube_ids import VIDEO_ID_RE

    if _login_required_redirect(request):
        raise HTTPException(status_code=401, detail="Login required")
    if not VIDEO_ID_RE.match(youtube_video_id or ""):
        raise HTTPException(status_code=400, detail="Not a YouTube video id")
    return _user_key(request)


@app.get("/api/captions/{youtube_video_id}", tags=["playback"], summary="Caption tracks for the player")
def api_caption_tracks(request: Request, youtube_video_id: str) -> dict[str, Any]:
    """Every caption track the player can offer, each served from this origin as WebVTT,
    and the choice the user last made in the player."""
    from . import captions

    user_key = _media_api_guard(request, youtube_video_id)
    preferences = db.get_playback_preferences(user_key)
    return {
        "ok": True,
        "youtube_video_id": youtube_video_id,
        "tracks": captions.list_tracks(youtube_video_id),
        "preference": {
            "enabled": bool(preferences.get("captions_enabled")),
            "language": str(preferences.get("captions_language") or ""),
            "auto": bool(preferences.get("captions_auto")),
        },
    }


@app.get("/api/captions/{youtube_video_id}/{track_id}.vtt", tags=["playback"], summary="One caption track as WebVTT")
def api_caption_vtt(request: Request, youtube_video_id: str, track_id: str) -> Response:
    """Fetched server-side from Invidious (or yt-dlp's source) and cleaned, so the browser
    never contacts YouTube or Invidious for a caption."""
    from . import captions

    _media_api_guard(request, youtube_video_id)
    try:
        text = captions.fetch_vtt(youtube_video_id, track_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="No such caption track")
    except captions.CaptionError as exc:
        raise HTTPException(status_code=502, detail=f"Caption source failed: {exc}")
    return Response(text, media_type="text/vtt; charset=utf-8", headers={"cache-control": "private, max-age=3600"})


def _live_chat_guard(request: Request, youtube_video_id: str) -> None:
    user_key = _media_api_guard(request, youtube_video_id)
    if not db.get_playback_preferences(user_key).get("live_chat_enabled"):
        raise HTTPException(status_code=403, detail="Live chat is off. Turn it on in Settings, Integrations.")


@app.get("/api/livechat/{youtube_video_id}", tags=["playback"], summary="Whether a video has live chat or a replay")
def api_live_chat_status(request: Request, youtube_video_id: str) -> dict[str, Any]:
    """live, replay, upcoming or none. Read server-side through yt-dlp, only with the
    Live chat setting on."""
    from . import livechat

    _live_chat_guard(request, youtube_video_id)
    return {"ok": True, "youtube_video_id": youtube_video_id, **livechat.status(youtube_video_id)}


@app.get("/api/livechat/{youtube_video_id}/replay", tags=["playback"], summary="Chat replay messages for a stretch of the video")
def api_live_chat_replay(request: Request, youtube_video_id: str, start: float = 0.0, end: float | None = None) -> dict[str, Any]:
    from . import livechat

    _live_chat_guard(request, youtube_video_id)
    return {"ok": True, **livechat.replay(youtube_video_id, start, end)}


@app.get("/api/livechat/{youtube_video_id}/live", tags=["playback"], summary="New live chat messages")
def api_live_chat_live(request: Request, youtube_video_id: str, after: int = 0) -> dict[str, Any]:
    """Messages after a sequence number. Polling keeps the server's follower running; it
    stops by itself shortly after the last poll."""
    from . import livechat

    _live_chat_guard(request, youtube_video_id)
    return {"ok": True, **livechat.live(youtube_video_id, max(0, int(after)))}


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
    view_text = _count_label(escape(views), "view") if views else "Views unavailable"
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
def api_comments(request: Request, youtube_video_id: str, continuation: str = "", sort: str = "") -> dict[str, Any]:
    """Fetch comments from Invidious. Off unless the user opts in.

    With a continuation token this returns one thread's replies instead of the top-level
    comments — Invidious serves both from the same endpoint. `sort` is passed through as
    Invidious's own sort_by, so Newest is YouTube's ordering rather than a re-sort of one
    page; anything but "new" stays on the default, top.
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
    params: dict[str, str] = {"continuation": token} if token else {}
    if sort == "new":
        params["sort_by"] = "new"
    try:
        with httpx.Client(timeout=max(5, settings.enrichment_timeout_seconds)) as client:
            response = client.get(
                f"{settings.invidious_base_url}/api/v1/comments/{youtube_video_id}",
                params=params or None,
            )
    except Exception as exc:
        logger.warning("comment fetch failed for %s: %s", youtube_video_id, exc)
        raise HTTPException(status_code=502, detail="Could not reach the comment source")
    notice = ""
    if response.status_code != 200 and not token and sort != "new":
        # Invidious (v2.20260723) fails top comments upstream for Shorts and for some long
        # videos, with HTTP 500 'Missing hash key: "commentRenderer"' while it parses YouTube's
        # newer comment layout. Its newest-first path parses fine, so fall back to that and
        # say so rather than show nothing. (yt-dlp has no comment path in this app.)
        try:
            with httpx.Client(timeout=max(5, settings.enrichment_timeout_seconds)) as client:
                retry = client.get(
                    f"{settings.invidious_base_url}/api/v1/comments/{youtube_video_id}",
                    params={"sort_by": "new"},
                )
            if retry.status_code == 200:
                response = retry
                notice = "Top comments are unavailable for this video, so these are the newest."
        except Exception as exc:  # noqa: BLE001 - the original failure is reported below
            logger.info("newest-comments fallback failed for %s: %s", youtube_video_id, exc)
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
        # Set when top comments failed upstream and these are the newest instead.
        "sort": "new" if notice or sort == "new" else "top",
        "notice": notice,
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

    This is the full list (limit=250), not the shell's VISIBLE + 1 probe: the
    subscribe/unsubscribe actions swap this markup in so the list refreshes in
    place without a full page reload. ("Show more" just unhides rows that are
    already in the shell.)"""
    auth_redirect = _login_required_redirect(request)
    if auth_redirect:
        return ""
    return _sidebar_subscription_group(limit=250, user_key=_user_key(request))


def _short_card(video: dict[str, Any], position: int) -> str:
    video_id = escape(str(video.get("youtube_video_id") or ""))
    title_text = str(video.get("title") or "Untitled")
    title = escape(_text_excerpt(title_text, 90))
    channel_text = str(video.get("channel_title") or video.get("author") or "Unknown channel")
    thumb = escape(_thumbnail_for(video))
    views = _compact_count(video.get("view_count"))
    meta = _count_label(views, "view") if views else _relative_time(video.get("published_at"))
    image = '<span class="thumb-fallback">OpenFeed</span>' + (
        f'<img src="{thumb}" alt="" loading="lazy" decoding="async" onerror="this.remove()">' if thumb else ""
    )
    return f'''
    <article class="short-card" data-video-position="{position}" data-dearrow-video="{video_id}" data-video-card data-video-id="{video_id}">
      <a class="short-thumb" href="/shorts?start={video_id}" aria-label="{title}">
        {image}
        <span class="short-badge">{_icon("shorts")}</span>
      </a>
      <div class="short-copy">
        <h3><a href="/shorts?start={video_id}">{title}</a></h3>
        <p>{escape(channel_text)}{f' &middot; {escape(meta)}' if meta else ''}</p>
        {_short_card_menu(video_id, title_text)}
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
        <div class="shelf" data-shelf>
          <div class="shelf-head">
            <div class="section-heading-copy"><h2 id="{heading_id}">Shorts</h2></div>
            <div class="shelf-actions">{_view_all_link("/shorts", "Shorts")}{_shelf_nav_buttons()}</div>
          </div>
          <div class="shelf-viewport">
            <div class="shorts-row" data-shelf-track>{cards}</div>
          </div>
        </div>
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
        row for row in _with_feed_uploads(db.list_subscription_uploads(fetch, user_key, include_shorts), fetch, user_key, include_shorts)
        if _notification_allows(row, levels.get(str(row.get("notify_key") or row.get("youtube_channel_id") or ""), "all"), preferred_topics)
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
    meta = " · ".join(part for part in (_count_label(views, "view") if views else "", published) if part)
    reaction = str(video.get("reaction") or "")
    like_selected = " is-selected" if reaction == "like" else ""
    dislike_selected = " is-selected" if reaction == "dislike" else ""
    like_label = _compact_count(video.get("like_count")) or "Like"

    def disc(icon: str) -> str:
        return f'<span class="shorts-disc">{_icon(icon)}</span>'

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
        <button class="shorts-tap" type="button" data-shorts-tap aria-label="Play or pause"></button>
        <div class="shorts-glyph" aria-hidden="true">{_icon("play")}</div>
        <div class="shorts-progress" aria-hidden="true"><span data-shorts-progress></span></div>
        <div class="shorts-overlay">
          <div class="shorts-byline">
            <a class="shorts-channel" href="{channel_href}">
              {_avatar_html(channel_text, video.get("channel_avatar_url"))}
              <span>{escape(channel_text)}</span>
            </a>
            {_shorts_subscribe_button(video)}
          </div>
          <h2 class="shorts-title"><a href="/watch/{video_id}">{title}</a></h2>
          <p class="shorts-meta">{escape(meta)}</p>
        </div>
      </div>
      <div class="shorts-rail" role="group" aria-label="Actions for {title}">
        <button class="shorts-action{like_selected}" type="button" data-feedback="like" data-video-id="{video_id}" aria-pressed="{'true' if reaction == 'like' else 'false'}" aria-label="Like">{disc("like")}<span>{escape(like_label)}</span></button>
        <button class="shorts-action{dislike_selected}" type="button" data-feedback="dislike" data-video-id="{video_id}" aria-pressed="{'true' if reaction == 'dislike' else 'false'}" aria-label="Dislike">{disc("dislike")}<span>Dislike</span></button>
        <button class="shorts-action" type="button" data-shorts-comments="{video_id}" aria-label="Comments">{disc("caption")}<span>Comments</span></button>
        <button class="shorts-action" type="button" data-shorts-mute aria-pressed="false" aria-label="Mute"><span class="shorts-disc"><span class="shorts-sound-on">{_icon("volume")}</span><span class="shorts-sound-off">{_icon("volume-x")}</span></span><span data-mute-label>Sound</span></button>
        <button class="shorts-action" type="button" data-save-to-playlist="{video_id}" data-video-title="{escape(_text_excerpt(title_text, 90), quote=True)}" aria-label="Save to playlist">{disc("playlist")}<span>Save</span></button>
        <button class="shorts-action" type="button" data-copy-url="/watch/{video_id}" aria-label="Share">{disc("share")}<span>Share</span></button>
        <a class="shorts-action" href="/watch/{video_id}" aria-label="Open full watch page">{disc("external")}<span>Open</span></a>
        {_shorts_slide_menu(video_id, title_text)}
      </div>
    </article>
    '''


def _shorts_page_html(videos: list[dict[str, Any]], user_key: str) -> str:
    slides = "".join(_shorts_slide(video, user_key) for video in _with_subscription_state(videos, user_key))
    try:
        comments_on = bool(db.get_feed_preferences(user_key).get("show_comments"))
    except Exception:  # noqa: BLE001 - the panel asks the API, which answers 403 when off
        comments_on = True
    if not slides:
        slides = _empty_state("shorts", "No Shorts yet", "Shorts appear here once your subscriptions upload them. Sync now, or turn Shorts on in Settings.")
    return f'''
      <h1 class="sr-only">Shorts</h1>
      <div class="shorts-viewport" data-shorts-viewport>
        <div class="shorts-stage" data-shorts-stage data-next-offset="{len(videos)}">
          {slides}
        </div>
        <div class="shorts-nav-buttons">
          <button class="shorts-nav" type="button" data-shorts-prev aria-label="Previous Short">{_icon("chevron-up")}</button>
          <button class="shorts-nav" type="button" data-shorts-next aria-label="Next Short">{_icon("chevron-down")}</button>
        </div>
        <aside class="shorts-comments" data-shorts-comment-panel data-comments-enabled="{"1" if comments_on else "0"}" hidden aria-label="Comments">
          <div class="comments-sheet-handle" data-shorts-comments-handle aria-hidden="true"></div>
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
    if _kids_profile(user_key):
        return _kids_refusal(request, "Shorts are off on kids profiles", "Switch to another profile to watch Shorts.", "shorts", 200)
    if _shorts_hidden(user_key):
        # Settings promises "Off hides them everywhere"; the viewer used to play on regardless.
        content = '<h1 class="sr-only">Shorts</h1>' + _shorts_off_state("/shorts?settings=content")
        return _app_shell("Shorts", "shorts", content, request=request)
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
    # Kids profiles have exclude_shorts forced on (db.get_feed_preferences), so this covers both.
    videos = [] if _shorts_hidden(user_key) else db.list_short_videos(max(1, min(limit, 24)), user_key, max(0, offset))
    return {
        "ok": True,
        "html": "".join(_shorts_slide(video, user_key) for video in _with_subscription_state(videos, user_key)),
        "count": len(videos),
        "next_offset": max(0, offset) + len(videos),
    }


@app.post("/api/sync/rss")
def api_sync_rss() -> dict[str, Any]:
    try:
        result = sync_rss_once(get_settings().rss_poll_limit)
    except Exception as exc:  # noqa: BLE001 - surfacing as 502, never a 500
        logger.exception("RSS sync failed: %s", exc)
        raise HTTPException(status_code=502, detail=f"RSS sync failed: {_safe_exc_name(exc)}") from exc
    if not result.get("ok"):
        raise HTTPException(status_code=502, detail=result)
    return result


@app.get("/feeds/recommended.xml")
def recommended_feed(request: Request) -> Response:
    # Library contents are protected by require_login/required_group everywhere else;
    # this feed used to be reachable anonymously and quietly leak titles + channels.
    if get_settings().require_login and not _current_user(request).get("authenticated"):
        return JSONResponse({"detail": "login_required"}, status_code=status.HTTP_401_UNAUTHORIZED)
    settings = get_settings()
    # The caller's own recommendations. This read the shared "local" user's before, so on
    # a signed-in install every account got the same list, built from someone else's data.
    videos = db.list_recommended_videos(50, user_key=_user_key(request))
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


# Sign-in, setup, Account & security and the admin's Users page (account_pages.py). Added
# as plain routes rather than include_router, which this FastAPI wraps in one lazy entry
# that has no .path for the route checks in the tests.
app.router.routes.extend(account_pages.router.routes)
# Passkeys, further OIDC providers and the trusted-header method (docs/accounts.md).
for _router in (oidc.router, passkeys.router, trusted_header.router):
    app.router.routes.extend(_router.routes)
# Admin, Server and its JSON twin (admin_pages.py).
admin_pages.install(app)
