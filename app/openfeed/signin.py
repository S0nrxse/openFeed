"""Sign-in: the method registry, server-side sessions and the request-safety checks.

How a sign-in runs (docs/accounts.md has the long version):

1. A method proves the first factor. The password method checks a form post; the OIDC
   method completes a redirect to the identity provider. Phase 2 adds passkeys and a
   trusted-header method the same way.
2. The method calls `complete_first_factor(request, account, method_key, next_path)`.
   If the account has two-step verification and the method does not already count as both
   factors (a passkey does), a short-lived signed "pending" cookie is set and the browser
   goes to /auth/2fa. Otherwise a session starts.
3. `start_session` stores a new session row and sets the cookie. The cookie value is
   `v2.<random>`; only its SHA-256 is stored.

Every request then reads the session through `user_from_request`, which refuses expired,
revoked and disabled-account sessions at once (one indexed query, cached on the request).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from fastapi import Request
from fastapi.responses import RedirectResponse, Response

from . import accounts, ratelimit
from .config import get_settings

logger = logging.getLogger("openfeed.auth")

SESSION_PREFIX = "v2."
PENDING_COOKIE = "openfeed_signin"
PENDING_SECONDS = 5 * 60
_SCOPE_KEY = "openfeed.session"
_UNSAFE = frozenset({"POST", "PUT", "PATCH", "DELETE"})
# Paths whose state-changing requests must prove they come from this site even without a
# session cookie (sign-in, setup and sign-up forms: login CSRF).
_ALWAYS_CHECKED = ("/auth/", "/setup", "/account", "/admin/users", "/admin/invites")


# --- the method registry -----------------------------------------------------------------


@dataclass
class MethodEntry:
    """What the sign-in page shows for a method: a button or a link."""

    key: str
    label: str
    href: str
    kind: str


@dataclass
class SignInMethod:
    """One way to sign in. Subclass, then `register(instance)`.

    key        stable id, also the `method` column of account_identities
               ('password', 'passkey', 'oidc:<provider>', 'header:<name>').
    kind       'password' | 'oidc' | 'passkey' | 'header'; groups methods in the UI.
    label      what people read ("Password", "Authentik", "Passkey").
    both_factors  True when the method already proves possession and knowledge (a passkey
               with user verification), so two-step verification is not asked again.

    Hooks (all optional except available):
    - available()                     on this install, with this configuration?
    - entry(next_path)                MethodEntry for the sign-in page, or None.
    - begin(request, next_path, intent)   start a ceremony; intent is 'signin', 'link' or
                                      'reauth'. Returns a Response (a redirect, a page).
    - finish(request)                 complete it; ends in complete_first_factor (signin),
                                      accounts.add_identity (link) or confirm_reauth.
    - describe(identity)              one row for "Sign-in methods" in Account & security.
    - can_link(account)               whether "Link" is offered for this account.
    - remove(account, identity)       unlink; the default deletes the identity row.
    """

    key: str = ""
    kind: str = ""
    label: str = ""
    both_factors: bool = False

    def available(self) -> bool:  # pragma: no cover - interface
        return False

    def entry(self, next_path: str) -> MethodEntry | None:
        return None

    async def begin(self, request: Request, next_path: str, intent: str = "signin") -> Response:  # pragma: no cover
        raise NotImplementedError

    async def finish(self, request: Request) -> Response:  # pragma: no cover
        raise NotImplementedError

    def describe(self, identity: dict[str, Any]) -> dict[str, Any]:
        return {"id": identity.get("id"), "method": self.key, "label": identity.get("label") or self.label,
                "kind": self.kind, "created_at": identity.get("created_at"), "last_used_at": identity.get("last_used_at")}

    def can_link(self, account: dict[str, Any], identities: list[dict[str, Any]]) -> bool:
        return False

    def link_href(self, next_path: str = "/account") -> str:
        return ""

    def remove(self, account: dict[str, Any], identity: dict[str, Any]) -> bool:
        return accounts.remove_identity(account["id"], identity["id"])

    def reauth_entry(self, identities: list[dict[str, Any]], next_path: str) -> MethodEntry | None:
        """How "Confirm it's you" offers this method, for an account with these identities.
        None when it cannot confirm (not linked, or not available here)."""
        return None


_REGISTRY: dict[str, SignInMethod] = {}
# Methods whose instances depend on configuration (one per OIDC provider) are kept current
# by loaders, which run before every registry read. A loader registers and unregisters.
_LOADERS: list[Callable[[], None]] = []


def register(method: SignInMethod) -> SignInMethod:
    _REGISTRY[method.key] = method
    return method


def unregister(key: str) -> None:
    _REGISTRY.pop(key, None)


def register_loader(loader: Callable[[], None]) -> Callable[[], None]:
    if loader not in _LOADERS:
        _LOADERS.append(loader)
    return loader


def _load() -> None:
    for loader in list(_LOADERS):
        try:
            loader()
        except Exception:  # noqa: BLE001 - a broken configuration must not break sign-in
            logger.exception("a sign-in method loader failed")


def method(key: str) -> SignInMethod | None:
    _load()
    found = _REGISTRY.get(key)
    if found:
        return found
    # A family registered under a prefix ('oidc:') answers for its members.
    prefix = key.split(":", 1)[0] + ":" if ":" in key else ""
    return _REGISTRY.get(prefix) if prefix else None


def methods() -> list[SignInMethod]:
    _load()
    return list(_REGISTRY.values())


def available_methods() -> list[SignInMethod]:
    _load()
    out = []
    for item in list(_REGISTRY.values()):
        try:
            if item.available():
                out.append(item)
        except Exception:  # noqa: BLE001 - a broken method must not break sign-in
            logger.exception("sign-in method %s failed its availability check", item.key)
    return out


# --- small helpers -----------------------------------------------------------------------


def _secret() -> bytes:
    settings = get_settings()
    return (settings.session_secret or settings.secret_key).encode("utf-8")


def secret_configured() -> bool:
    return bool(_secret())


def token_hash(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def new_token(prefix: str = SESSION_PREFIX) -> str:
    return f"{prefix}{secrets.token_urlsafe(32)}"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode((value + "=" * (-len(value) % 4)).encode("ascii"))


def sign(purpose: str, payload: dict[str, Any]) -> str:
    """HMAC-signed, purpose-bound payload. Not encrypted: put nothing secret in it."""
    body = _b64(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    mac = hmac.new(_secret(), f"{purpose}.{body}".encode("ascii"), hashlib.sha256).digest()
    return f"{body}.{_b64(mac)}"


def unsign(purpose: str, value: str | None) -> dict[str, Any] | None:
    if not value or "." not in value or not _secret():
        return None
    body, mac = value.rsplit(".", 1)
    expected = _b64(hmac.new(_secret(), f"{purpose}.{body}".encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(mac, expected):
        return None
    try:
        payload = json.loads(_unb64(body))
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or int(payload.get("exp", 0)) <= int(time.time()):
        return None
    return payload


def cookie_secure() -> bool:
    override = get_settings().cookie_secure.strip().lower()
    if override in {"1", "true", "yes", "on"}:
        return True
    if override in {"0", "false", "no", "off"}:
        return False
    return get_settings().public_url.startswith("https://")


def safe_next(next_path: str | None, fallback: str = "/") -> str:
    parsed = urllib.parse.urlsplit(next_path or fallback)
    if parsed.scheme or parsed.netloc or not parsed.path.startswith("/") or "\\" in parsed.path or parsed.path.startswith("//"):
        return fallback
    # Never bounce back into a sign-in step.
    if parsed.path.startswith(("/auth/login", "/auth/2fa", "/auth/callback", "/auth/logout", "/auth/oidc/", "/auth/passkey/", "/auth/header/")) or parsed.path == "/setup":
        return fallback
    return urllib.parse.urlunsplit(("", "", parsed.path, parsed.query, parsed.fragment))


_BROWSERS = (
    ("SamsungBrowser", "Samsung Internet"), ("EdgA", "Edge"), ("Edg/", "Edge"), ("OPR/", "Opera"), ("Opera", "Opera"),
    ("Vivaldi", "Vivaldi"), ("Brave", "Brave"), ("YaBrowser", "Yandex"), ("Firefox/", "Firefox"), ("FxiOS", "Firefox"),
    ("CriOS", "Chrome"), ("Chrome/", "Chrome"), ("Chromium", "Chromium"), ("Safari/", "Safari"),
)
_SYSTEMS = (
    ("Android", "Android"), ("iPhone", "iPhone"), ("iPad", "iPad"), ("CrOS", "ChromeOS"), ("Windows", "Windows"),
    ("Macintosh", "macOS"), ("Mac OS X", "macOS"), ("Linux", "Linux"),
)


def device_label(user_agent: str | None) -> str:
    """A coarse, human label like "Firefox on Android". Nothing finer is stored."""
    ua = str(user_agent or "")
    if not ua:
        return "Unknown device"
    system = next((name for needle, name in _SYSTEMS if needle in ua), "")
    if "; wv)" in ua or " wv)" in ua or "OpenFeedApp" in ua:
        browser = "OpenFeed app"
    elif "curl/" in ua or "python-" in ua.lower() or "httpx" in ua.lower():
        browser = "Script"
    else:
        browser = next((name for needle, name in _BROWSERS if needle in ua), "Browser")
    return f"{browser} on {system}" if system else browser


def _epoch(value: Any) -> int:
    if isinstance(value, datetime):
        return int(value.timestamp())
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


# --- sessions ----------------------------------------------------------------------------


def session_token(request: Request | None) -> str:
    cookies = getattr(request, "cookies", None) or {}
    value = str(cookies.get(get_settings().session_cookie) or "")
    return value if value.startswith(SESSION_PREFIX) else ""


def user_from_request(request: Request | None) -> dict[str, Any] | None:
    """The signed-in account behind this request's session cookie, or None.

    Cached on the ASGI scope: the access middleware, the shell and the handler all ask. A
    database failure reads as signed out, which fails closed.
    """
    token = session_token(request)
    if not token:
        return None
    scope = getattr(request, "scope", None)
    cache = scope.setdefault(_SCOPE_KEY, {}) if isinstance(scope, dict) else None
    if cache is not None and token in cache:
        return dict(cache[token]) if cache[token] else None
    user = None
    try:
        row = accounts.session_by_hash(token_hash(token))
        if row:
            user = _session_user(row)
            # The idle window slides at most once a minute; most requests write nothing.
            if int(time.time()) - _epoch(row.get("last_seen_at")) >= 60:
                try:
                    accounts.touch_session(row["id"], row["account_id"], get_settings().session_idle_seconds)
                except Exception:  # noqa: BLE001 - a missed touch only shortens the idle window
                    pass
    except Exception:  # noqa: BLE001 - fail closed
        logger.warning("session lookup failed; treating the request as signed out", exc_info=True)
        user = None
    if cache is not None:
        cache[token] = user
    return dict(user) if user else None


def adopt_session_cookie(request: Request, token: str) -> None:
    """Make the rest of this request see a session that was just created for it.

    Used where a session starts inside the request it answers (a trusted proxy header):
    the cookie is written into this request's headers, and harden_http copies it onto the
    response from scope["openfeed.upgraded_session"].
    """
    cookie_name = get_settings().session_cookie
    others = "; ".join(f"{k}={v}" for k, v in (request.cookies or {}).items() if k != cookie_name)
    headers = [(k, v) for k, v in request.scope.get("headers", []) if k != b"cookie"]
    headers.append((b"cookie", (f"{cookie_name}={token}" + ("; " + others if others else "")).encode("latin-1")))
    request.scope["headers"] = headers
    request.scope["openfeed.upgraded_session"] = token
    for attr in ("_cookies", "_headers"):
        request.__dict__.pop(attr, None)
    forget_cached_session(request)


def forget_cached_session(request: Request | None) -> None:
    scope = getattr(request, "scope", None)
    if isinstance(scope, dict):
        scope.pop(_SCOPE_KEY, None)


def _session_user(row: dict[str, Any]) -> dict[str, Any]:
    methods = [str(m) for m in (row.get("methods") or [])]
    groups = row.get("idp_groups") if isinstance(row.get("idp_groups"), list) else []
    return {
        "authenticated": True,
        "user_key": str(row["user_key"]),
        "sub": str(row["user_key"]),
        "email": "",
        "display_name": str(row.get("display_name") or row.get("username") or "OpenFeed user"),
        "username": str(row.get("username") or ""),
        "groups": [str(g) for g in groups],
        "iat": _epoch(row.get("created_at")),
        "session_backed": True,
        "session_id": str(row["id"]),
        "account_id": str(row["account_id"]),
        "role": str(row.get("role") or "member"),
        "reauth_at": _epoch(row.get("reauth_at")),
        "methods": methods,
        "idp": any(m.startswith("oidc:") or m.startswith("header:") for m in methods),
        "totp_enabled": bool(row.get("totp_enabled_at")),
    }


def set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        get_settings().session_cookie, token, max_age=get_settings().auth_session_ttl_seconds,
        httponly=True, secure=cookie_secure(), samesite="lax", path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(get_settings().session_cookie, path="/", secure=cookie_secure(), samesite="lax")


def start_session(
    request: Request, account: dict[str, Any], methods_used: list[str], next_path: str,
    *, idp_groups: list[str] | None = None, response: Response | None = None,
) -> Response:
    """Create a session for account and answer with a redirect that sets its cookie.

    Always a new token: an existing session cookie on this browser (another account's, or
    an older one of this account) is ended, so a planted cookie cannot be carried across a
    sign-in (session fixation).
    """
    settings = get_settings()
    old = session_token(request)
    if old:
        try:
            accounts.delete_session_by_hash(token_hash(old))
        except Exception:  # noqa: BLE001
            pass
    token = new_token()
    accounts.create_session(
        account["id"], token_hash(token), device=device_label(request.headers.get("user-agent")),
        methods=methods_used, idp_groups=idp_groups or [], idle_seconds=settings.session_idle_seconds,
        max_seconds=settings.auth_session_ttl_seconds, reauth=True,
    )
    forget_cached_session(request)
    out = response or RedirectResponse(safe_next(next_path), status_code=303)
    set_session_cookie(out, token)
    out.delete_cookie(PENDING_COOKIE, path="/")
    # A new account on this browser starts at "Who's watching", not on the last one's profile.
    out.delete_cookie("openfeed_profile", path="/")
    out.headers["Cache-Control"] = "no-store"
    return out


def end_session(request: Request, response: Response) -> None:
    token = session_token(request)
    if token:
        try:
            accounts.delete_session_by_hash(token_hash(token))
        except Exception:  # noqa: BLE001
            logger.warning("could not delete the session row on sign-out", exc_info=True)
    clear_session_cookie(response)
    response.delete_cookie(PENDING_COOKIE, path="/")


def rotate(request: Request, user: dict[str, Any], response: Response, *, reauth: bool = False) -> None:
    """New token for the current session (privilege change, re-authentication)."""
    token = new_token()
    if accounts.rotate_session(user["session_id"], token_hash(token), reauth=reauth):
        set_session_cookie(response, token)
    forget_cached_session(request)


def reauth_fresh(user: dict[str, Any] | None) -> bool:
    if not user or not user.get("session_backed"):
        return False
    window = max(30, get_settings().reauth_seconds)
    return int(time.time()) - int(user.get("reauth_at") or 0) <= window


def idp_policy(user: dict[str, Any]) -> tuple[frozenset[str], str]:
    """(admin groups, required group) for the identity provider or proxy behind a session.

    Each OIDC provider has its own (OPENFEED_OIDC_<NAME>_ADMIN_GROUPS and _REQUIRED_GROUP),
    so a group name at one provider never grants anything at another. The provider made
    from the single-provider settings, and sessions with no provider at all, use
    OPENFEED_ADMIN_GROUPS and OPENFEED_REQUIRED_GROUP as before.
    """
    settings = get_settings()
    for key in user.get("methods") or []:
        if str(key).startswith(("oidc:", "header:")):
            policy = getattr(method(str(key)), "group_policy", None)
            if callable(policy):
                return policy()
            break
    return frozenset(settings.admin_groups), settings.required_group.strip()


def is_admin(user: dict[str, Any]) -> bool:
    """Admin for a session: the role in the database, or an IdP admin group.

    IdP groups are a snapshot from sign-in, so they grant admin only for
    OPENFEED_ADMIN_SESSION_SECONDS after it, as before accounts existed. An
    authentik_oidc install with no admin groups keeps its old rule: everyone who signed in
    through its one provider administers their own server.
    """
    settings = get_settings()
    if user.get("role") == "admin":
        return True
    admin_groups, _required = idp_policy(user)
    if user.get("idp") and admin_groups:
        age_ok = settings.admin_session_seconds <= 0 or int(time.time()) - int(user.get("iat") or 0) <= settings.admin_session_seconds
        if age_ok and set(user.get("groups") or []) & admin_groups:
            return True
    idp_methods = [str(m) for m in user.get("methods") or [] if str(m).startswith(("oidc:", "header:"))]
    legacy = not idp_methods or OIDC.key in idp_methods
    if settings.auth_mode == "authentik_oidc" and user.get("idp") and legacy and (not settings.require_login or not settings.admin_groups):
        return True
    return False


def usable(identity: dict[str, Any]) -> bool:
    """Whether this linked identity can sign in on this install as configured now."""
    item = method(str(identity.get("method") or ""))
    if not item or item.key != identity.get("method"):
        return False
    try:
        return bool(item.available())
    except Exception:  # noqa: BLE001
        return False


def can_remove(identities: list[dict[str, Any]], identity_id: str) -> bool:
    """Never leave an account without a way in: something else usable must remain."""
    return any(usable(other) for other in identities if str(other.get("id")) != str(identity_id))


# --- first factor done: second step or session -------------------------------------------


def complete_first_factor(
    request: Request, account: dict[str, Any], method_key: str, next_path: str,
    *, idp_groups: list[str] | None = None, both_factors: bool = False,
) -> Response:
    """Called by every method once its first factor checks out."""
    if account.get("totp_enabled_at") and not both_factors:
        payload = {
            "aid": str(account["id"]), "m": method_key, "g": list(idp_groups or [])[:50],
            "next": safe_next(next_path), "exp": int(time.time()) + PENDING_SECONDS, "n": secrets.token_hex(8),
        }
        response = RedirectResponse("/auth/2fa", status_code=303)
        response.set_cookie(PENDING_COOKIE, sign("pending", payload), max_age=PENDING_SECONDS, httponly=True,
                            secure=cookie_secure(), samesite="lax", path="/")
        response.headers["Cache-Control"] = "no-store"
        return response
    used = [method_key] + (["passkey-uv"] if both_factors and method_key == "passkey" else [])
    return start_session(request, account, used, next_path, idp_groups=idp_groups)


def pending_sign_in(request: Request) -> dict[str, Any] | None:
    return unsign("pending", (getattr(request, "cookies", None) or {}).get(PENDING_COOKIE))


# --- request safety ----------------------------------------------------------------------


def _origin_host(value: str) -> str:
    parsed = urllib.parse.urlsplit(value.strip())
    return parsed.netloc.lower() if parsed.scheme in {"http", "https"} else ""


def allowed_hosts(request: Request) -> set[str]:
    forwarded = str(request.headers.get("x-forwarded-host") or "").split(",", 1)[0].strip()
    hosts = {(forwarded or str(request.headers.get("host") or "")).lower()}
    public = urllib.parse.urlsplit(get_settings().public_url).netloc.lower()
    if public:
        hosts.add(public)
    return {h for h in hosts if h}


def csrf_failure(request: Request) -> bool:
    """True when a state-changing request carrying a session cookie (or aimed at a sign-in
    form) cannot show it came from this site. Browsers send Origin on every POST, PUT,
    PATCH and DELETE; Referer is accepted when a privacy setting drops Origin. A request
    with neither is refused: no browser of ours sends one, and a script uses a token."""
    if request.method not in _UNSAFE:
        return False
    path = request.url.path
    if not session_token(request) and not path.startswith(_ALWAYS_CHECKED):
        return False
    hosts = allowed_hosts(request)
    origin = str(request.headers.get("origin") or "").strip()
    if origin:
        return _origin_host(origin) not in hosts
    referer = str(request.headers.get("referer") or "").strip()
    if referer:
        return _origin_host(referer) not in hosts
    return True


# --- legacy cookies from before server-side sessions ---------------------------------------


def exchange_legacy_cookie(request: Request, payload: dict[str, Any], provider_key: str) -> str | None:
    """Trade a pre-accounts signed Authentik cookie for a server session, once.

    Existing authentik_oidc installs keep their users signed in across the upgrade. The
    exchange is single-use per cookie (remembered in Redis for its remaining lifetime), the
    account must not be disabled, and the session keeps the original sign-in time so the
    admin-group age limit still counts from the real sign-in.
    """
    sub = str(payload.get("sub") or "").strip()
    expires = int(payload.get("expires_at") or 0)
    now = int(time.time())
    if not sub or expires <= now:
        return None
    raw = str((getattr(request, "cookies", None) or {}).get(get_settings().session_cookie) or "")
    if not ratelimit.remember_once(f"of:auth:legacy:{token_hash(raw)}", expires - now):
        return None
    identity = accounts.find_identity(provider_key, sub)
    if identity:
        account = identity
    else:
        account = accounts.get_by_user_key(sub)
        if account:
            accounts.add_identity(account["id"], provider_key, sub, get_settings().oidc_provider_name)
        else:
            account = accounts.create_account(
                accounts.unique_username(str(payload.get("username") or payload.get("name") or "user")),
                str(payload.get("name") or ""), user_key=sub, identity=(provider_key, sub, get_settings().oidc_provider_name),
            )
    if account.get("disabled"):
        return None
    groups = payload.get("groups") if isinstance(payload.get("groups"), list) else []
    token = new_token()
    settings = get_settings()
    accounts.create_session(
        account["id"], token_hash(token), device=device_label(request.headers.get("user-agent")),
        methods=[provider_key], idp_groups=[str(g) for g in groups], idle_seconds=settings.session_idle_seconds,
        max_seconds=max(60, expires - now), reauth=False,
        signed_in_at=int(payload.get("iat") or 0) or None,
    )
    return token


# --- the setup code ------------------------------------------------------------------------

_SETUP_KEY = "of:auth:setup-code"
_memory_setup_code: dict[str, str] = {}


def setup_code(create: bool = True) -> str:
    """The one-time code /setup asks for, shared by every app process through Redis."""
    configured = get_settings().setup_code.strip()
    if configured:
        return configured
    existing = ratelimit.get(_SETUP_KEY) or _memory_setup_code.get("code")
    if existing or not create:
        return existing or ""
    code = "-".join(secrets.token_hex(2) for _ in range(3))
    if not ratelimit.put(_SETUP_KEY, code):
        stored = ratelimit.get(_SETUP_KEY)
        if stored:
            return stored
    _memory_setup_code["code"] = code
    # stdout, so it is in `docker compose logs` whatever the logging setup.
    print(f"OpenFeed has no admin yet. Open /setup and enter this one-time setup code: {code}", flush=True)
    return code


def announce_setup_code() -> None:
    """Write the setup code to the server log, at startup, while no admin exists."""
    try:
        if accounts.admin_exists():
            return
    except Exception:  # noqa: BLE001 - no database yet: nothing to announce
        return
    code = setup_code(create=False)
    if code:
        print(f"OpenFeed has no admin yet. Open /setup and enter this one-time setup code: {code}", flush=True)
    else:
        setup_code()  # creates and prints one


def forget_setup_code() -> None:
    _memory_setup_code.clear()
    try:
        ratelimit._redis().delete(_SETUP_KEY)
    except Exception:  # noqa: BLE001
        pass


def setup_code_matches(candidate: str) -> bool:
    expected = setup_code(create=False)
    given = re.sub(r"[^0-9a-f]", "", str(candidate or "").lower())
    return bool(expected) and hmac.compare_digest(re.sub(r"[^0-9a-f]", "", expected.lower()), given)


# --- the built-in methods ------------------------------------------------------------------


class PasswordMethod(SignInMethod):
    """Username and password. The form itself is on the sign-in page (account_pages)."""

    def __init__(self) -> None:
        super().__init__(key="password", kind="password", label="Password")

    def available(self) -> bool:
        return get_settings().password_signin_enabled

    def describe(self, identity: dict[str, Any]) -> dict[str, Any]:
        row = super().describe(identity)
        row["label"] = "Password"
        return row


class OidcMethod(SignInMethod):
    """An OpenID Connect provider. This base instance is the provider configured by the
    single-provider settings (OPENFEED_OIDC_* / AUTHENTIK_*), key 'oidc:authentik'.

    oidc.py replaces it in the registry with one oidc.ProviderMethod per configured provider
    (the protocol steps live there); this object stays as the fallback that names old
    identities and as `OIDC.key` for code that means the original provider.
    """

    def __init__(self, name: str = "authentik", display: str = "") -> None:
        super().__init__(key=f"oidc:{name}", kind="oidc", label=display or name.title())
        self.name = name
        self._display = display

    def available(self) -> bool:
        settings = get_settings()
        return settings.sign_in_enabled and settings.auth_configured

    @property
    def display(self) -> str:
        return self._display or get_settings().oidc_provider_name or "Authentik"

    def start_href(self, next_path: str, intent: str = "signin") -> str:
        query = {"next": safe_next(next_path)}
        if intent != "signin":
            query["intent"] = intent
        return f"/auth/oidc/{self.name}/start?{urllib.parse.urlencode(query)}"

    def entry(self, next_path: str) -> MethodEntry | None:
        return MethodEntry(self.key, f"Sign in with {self.display}", self.start_href(next_path), self.kind)

    def describe(self, identity: dict[str, Any]) -> dict[str, Any]:
        row = super().describe(identity)
        row["label"] = self.display
        return row

    def can_link(self, account: dict[str, Any], identities: list[dict[str, Any]]) -> bool:
        return self.available() and not any(i.get("method") == self.key for i in identities)

    def link_href(self, next_path: str = "/account") -> str:
        return self.start_href(next_path, "link")

    def reauth_entry(self, identities: list[dict[str, Any]], next_path: str) -> MethodEntry | None:
        if not self.available() or not any(i.get("method") == self.key for i in identities):
            return None
        return MethodEntry(self.key, f"Confirm with {self.display}", self.start_href(next_path, "reauth"), self.kind)


PASSWORD = register(PasswordMethod())
OIDC = register(OidcMethod())


def labels_for(method_keys: list[str]) -> list[str]:
    out = []
    for key in method_keys:
        item = method(key)
        if key == "password":
            out.append("Password")
        elif key == "passkey":
            out.append("Passkey")
        elif isinstance(item, OidcMethod):
            out.append(item.display if item.key == key else key.split(":", 1)[-1].title())
        elif item:
            out.append(str(getattr(item, "display", "") or item.label))
        else:
            out.append(key.split(":", 1)[-1].title())
    return out


__all__ = [
    "SignInMethod", "MethodEntry", "register", "method", "methods", "available_methods",
    "complete_first_factor", "start_session", "end_session", "user_from_request", "reauth_fresh",
    "csrf_failure", "device_label", "token_hash",
]
