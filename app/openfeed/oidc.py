"""OpenID Connect sign-in with any number of providers (docs/accounts.md, "OIDC providers").

Configuration
-------------
- `OPENFEED_OIDC_PROVIDERS=authentik,pocketid` names the providers. Each reads
  `OPENFEED_OIDC_<NAME>_ISSUER`, `_CLIENT_ID`, `_CLIENT_SECRET`, `_DISPLAY_NAME`, `_SCOPES`,
  `_ADMIN_GROUPS`, `_REQUIRED_GROUP`, `_GROUPS_CLAIM`, `_CREATE_ACCOUNTS`, `_REDIRECT_URI`,
  `_DISCOVERY_URL`, `_BACKCHANNEL_BASE_URL` and `_TOKEN_AUTH` (NAME upper-cased, `-` as `_`).
- The single-provider settings from before (`OPENFEED_OIDC_ISSUER`, `_CLIENT_ID`, ... and
  the `AUTHENTIK_OIDC_*` spellings) still make one provider, `authentik`, with the method
  key `oidc:authentik`, callback `/auth/callback` and `OPENFEED_ADMIN_GROUPS`. Existing
  identities, and the user_keys of accounts first made by an Authentik sign-in, stay put.
  Listing `authentik` in OPENFEED_OIDC_PROVIDERS keeps those settings as its defaults.

The protocol
------------
Authorization code flow with PKCE (S256), `state` and `nonce`:

1. `start` makes a random state, nonce and code verifier and keeps them on the server
   (authstate, 10 minutes, single use). The browser gets a signed cookie naming the state,
   which binds the flow to the browser that started it (login CSRF).
2. `callback` takes the flow by its state (a second use of the same state finds nothing,
   so a replayed callback stops here before the code is spent), checks the cookie, swaps
   the code at the token endpoint with the verifier, and verifies the ID token: signature
   against the provider's JWKS (or, for HS256, the client secret, which is how Authentik
   signs when no key is chosen), issuer, audience (and azp), expiry, issued-at and nonce.
3. Claims come from the verified ID token. When the provider has a userinfo endpoint it is
   asked too (groups and names are often only there), and its `sub` must equal the ID
   token's, as the OIDC spec requires.

Why the ID token rather than only userinfo, as before: userinfo answers whoever holds the
access token, and nothing ties it to this browser's request. The signed ID token carries
the nonce this flow made, names this client as its audience, and names the issuer.

Accounts
--------
- A known identity signs in. Linking (Account & security, after "Confirm it's you") adds
  the provider to the signed-in account.
- An unknown identity gets a new account only when that is allowed: registration is open,
  the person arrived through an invite link, or the provider is set to
  `_CREATE_ACCOUNTS=always`. Otherwise the page says sign-in isn't open. No account is ever
  matched by email address or username.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import re
import secrets
import threading
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any

import httpx
import jwt
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

from . import accounts, authstate, ratelimit, signin
from .config import get_settings

logger = logging.getLogger("openfeed.auth")
router = APIRouter(include_in_schema=False)

LEGACY = "authentik"
FLOW_SECONDS = 10 * 60
CACHE_SECONDS = 3600
LEEWAY_SECONDS = 60
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_ASYMMETRIC = ("RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512", "EdDSA")
_SYMMETRIC = ("HS256", "HS384", "HS512")
_KNOWN_NAMES = {
    "authentik": "Authentik", "pocketid": "Pocket ID", "pocket-id": "Pocket ID", "authelia": "Authelia",
    "keycloak": "Keycloak", "zitadel": "Zitadel", "google": "Google", "gitlab": "GitLab", "kanidm": "Kanidm",
    "github": "GitHub", "microsoft": "Microsoft", "forgejo": "Forgejo", "gitea": "Gitea", "lldap": "LLDAP",
}


class OidcError(Exception):
    """A refused sign-in. code is stable (tests, logs); message is the sentence shown."""

    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(code)
        self.code = code
        self.message = message
        self.status_code = status_code


# --- configuration -------------------------------------------------------------------------


@dataclass(frozen=True)
class Provider:
    name: str
    issuer: str
    client_id: str
    client_secret: str
    display_name: str
    scopes: str
    admin_groups: frozenset[str]
    required_group: str
    groups_claim: str
    create_accounts: str  # 'registration' | 'always'
    redirect_uri: str
    discovery_url: str
    backchannel_base_url: str
    token_auth: str  # '' (from discovery) | 'client_secret_basic' | 'client_secret_post' | 'none'
    legacy: bool  # made from the single-provider settings: sub is the user_key, /auth/callback

    @property
    def key(self) -> str:
        return f"oidc:{self.name}"

    @property
    def callback_path(self) -> str:
        return "/auth/callback" if self.legacy else f"/auth/oidc/{self.name}/callback"


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def _groups(value: str) -> frozenset[str]:
    return frozenset(part.strip() for part in value.split(",") if part.strip())


def _create_policy(value: str, default: str) -> str:
    value = value.strip().lower()
    return value if value in {"registration", "always"} else default


def _legacy_values() -> dict[str, Any]:
    settings = get_settings()
    return {
        "issuer": settings.oidc_issuer, "client_id": settings.oidc_client_id, "client_secret": settings.oidc_client_secret,
        "display_name": settings.oidc_provider_name or "Authentik", "scopes": settings.oidc_scope,
        "admin_groups": frozenset(settings.admin_groups), "required_group": settings.required_group.strip(),
        "redirect_uri": settings.oidc_redirect_uri, "discovery_url": settings.oidc_discovery_url,
        "backchannel_base_url": settings.oidc_backchannel_base_url,
        # authentik_oidc installs made an account for everyone their provider let through;
        # they keep doing so. With built-in accounts, registration decides.
        "create_accounts": _create_policy(_env("OPENFEED_OIDC_CREATE_ACCOUNTS"), "always" if settings.auth_mode == "authentik_oidc" else "registration"),
        "token_auth": _env("OPENFEED_OIDC_TOKEN_AUTH").lower() or "client_secret_post",
        "groups_claim": _env("OPENFEED_OIDC_GROUPS_CLAIM") or "groups",
    }


def _named(name: str) -> Provider | None:
    prefix = f"OPENFEED_OIDC_{name.upper().replace('-', '_')}_"
    legacy = name == LEGACY and bool(get_settings().oidc_issuer)
    base = _legacy_values() if legacy else {}

    def pick(field: str, env: str, default: str = "") -> str:
        return _env(prefix + env) or str(base.get(field) or default)

    issuer = pick("issuer", "ISSUER").rstrip("/")
    client_id = pick("client_id", "CLIENT_ID")
    if not issuer or not client_id:
        logger.warning("OIDC provider %s is listed but has no %sISSUER or %sCLIENT_ID; skipped", name, prefix, prefix)
        return None
    admin = _env(prefix + "ADMIN_GROUPS")
    return Provider(
        name=name, issuer=issuer, client_id=client_id, client_secret=pick("client_secret", "CLIENT_SECRET"),
        display_name=pick("display_name", "DISPLAY_NAME", _KNOWN_NAMES.get(name, name.replace("-", " ").title())),
        scopes=pick("scopes", "SCOPES", "openid email profile"),
        admin_groups=_groups(admin) if admin else frozenset(base.get("admin_groups") or ()),
        required_group=pick("required_group", "REQUIRED_GROUP"),
        groups_claim=pick("groups_claim", "GROUPS_CLAIM", "groups"),
        create_accounts=_create_policy(_env(prefix + "CREATE_ACCOUNTS"), str(base.get("create_accounts") or "registration")),
        redirect_uri=pick("redirect_uri", "REDIRECT_URI"), discovery_url=pick("discovery_url", "DISCOVERY_URL"),
        backchannel_base_url=pick("backchannel_base_url", "BACKCHANNEL_BASE_URL").rstrip("/"),
        token_auth=pick("token_auth", "TOKEN_AUTH").lower(), legacy=legacy,
    )


def providers() -> list[Provider]:
    """Every configured provider, the single-provider one first. Empty with sign-in off."""
    settings = get_settings()
    if not settings.sign_in_enabled or not signin.secret_configured():
        return []
    out: list[Provider] = []
    seen: set[str] = set()
    for raw in _env("OPENFEED_OIDC_PROVIDERS").split(","):
        name = raw.strip().lower()
        if not name:
            continue
        if not _NAME_RE.match(name) or name in seen:
            logger.warning("OPENFEED_OIDC_PROVIDERS: %r is not a usable provider name; skipped", raw.strip())
            continue
        seen.add(name)
        provider = _named(name)
        if provider:
            out.append(provider)
    if LEGACY not in seen and settings.auth_configured:
        values = _legacy_values()
        out.insert(0, Provider(name=LEGACY, legacy=True, required_group=values.pop("required_group"), **{
            k: (v.rstrip("/") if k in {"issuer", "backchannel_base_url"} else v) for k, v in values.items()}))
    return out


def provider(name: str | None) -> Provider | None:
    found = providers()
    if not name:
        return found[0] if found else None
    return next((p for p in found if p.name == name), None)


# --- the registry entries ---------------------------------------------------------------


class ProviderMethod(signin.OidcMethod):
    """One configured provider as the registry, the sign-in page and Account & security
    see it."""

    def __init__(self, config: Provider) -> None:
        super().__init__(config.name, config.display_name)
        self.config = config

    def available(self) -> bool:
        return provider(self.name) is not None

    def group_policy(self) -> tuple[frozenset[str], str]:
        return self.config.admin_groups, self.config.required_group


_loaded: dict[str, Provider] = {}
_load_lock = threading.Lock()


def _sync_registry() -> None:
    """Keep one ProviderMethod per configured provider in signin's registry."""
    current = {p.name: p for p in providers()}
    with _load_lock:
        if current == _loaded:
            return
        for name in list(_loaded):
            if name not in current:
                if name == LEGACY:
                    signin.register(signin.OIDC)
                else:
                    signin.unregister(f"oidc:{name}")
        for name, config in current.items():
            signin.register(ProviderMethod(config))
        _loaded.clear()
        _loaded.update(current)


signin.register_loader(_sync_registry)


# --- discovery and keys ---------------------------------------------------------------------

_discovery_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_jwks_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_cache_lock = threading.Lock()


def _rewrite_origin(endpoint: str, base_url: str) -> str:
    if not endpoint or not base_url:
        return endpoint
    parsed = urllib.parse.urlsplit(endpoint)
    base = urllib.parse.urlsplit(base_url)
    if not parsed.scheme or not base.scheme or not base.netloc:
        return endpoint
    return urllib.parse.urlunsplit((base.scheme, base.netloc, parsed.path, parsed.query, parsed.fragment))


def _same_issuer(a: str, b: str) -> bool:
    return a.rstrip("/") == b.rstrip("/")


def expected_issuers(config: Provider) -> list[str]:
    """The issuer, with and without a trailing slash. With a back-channel base URL the
    provider may name itself by the address OpenFeed reached it at (Authentik does), so
    that spelling counts too: the admin configured both."""
    out = {config.issuer, config.issuer + "/"}
    if config.backchannel_base_url:
        inner = _rewrite_origin(config.issuer, config.backchannel_base_url)
        out |= {inner, inner + "/"}
    return sorted(out)


def discovery(config: Provider) -> dict[str, Any]:
    url = config.discovery_url or urllib.parse.urljoin(config.issuer + "/", ".well-known/openid-configuration")
    now = time.time()
    with _cache_lock:
        cached = _discovery_cache.get(config.name + "|" + url)
    if cached and cached[0] > now:
        return cached[1]
    try:
        answer = httpx.get(url, timeout=8.0, headers={"Accept": "application/json"})
        answer.raise_for_status()
        payload = answer.json()
    except Exception as exc:  # noqa: BLE001
        raise OidcError("discovery_unavailable", f"OpenFeed could not reach {config.display_name}'s discovery document. "
                        "Check that the provider is running and that its issuer URL is right.", 503) from exc
    if not isinstance(payload, dict):
        raise OidcError("discovery_invalid", f"{config.display_name} answered, but its discovery document could not be read.", 503)
    if payload.get("issuer") and not any(_same_issuer(str(payload["issuer"]), i) for i in expected_issuers(config)):
        raise OidcError("discovery_issuer", f"{config.display_name}'s discovery document names a different issuer than the one configured.", 503)
    doc = dict(payload)
    doc["issuer"] = config.issuer
    # The browser goes to the public address; the server may reach the provider by an
    # inner one (OPENFEED_OIDC_BACKCHANNEL_BASE_URL), as before.
    for field in ("authorization_endpoint", "end_session_endpoint"):
        if doc.get(field) and config.legacy:
            doc[field] = _rewrite_origin(str(doc[field]), config.issuer)
    for field in ("token_endpoint", "userinfo_endpoint", "jwks_uri"):
        if doc.get(field) and config.backchannel_base_url:
            doc[field] = _rewrite_origin(str(doc[field]), config.backchannel_base_url)
    if not doc.get("authorization_endpoint") or not doc.get("token_endpoint"):
        raise OidcError("endpoints_unavailable", f"{config.display_name}'s discovery document does not list the endpoints OpenFeed needs.", 503)
    with _cache_lock:
        _discovery_cache[config.name + "|" + url] = (now + CACHE_SECONDS, doc)
    return doc


def _jwks(config: Provider, doc: dict[str, Any], *, refresh: bool = False) -> dict[str, Any]:
    uri = str(doc.get("jwks_uri") or "")
    if not uri:
        raise OidcError("jwks_missing", f"{config.display_name} does not publish signing keys (jwks_uri).", 503)
    now = time.time()
    with _cache_lock:
        cached = _jwks_cache.get(uri)  # (fetched at, keys)
    if cached:
        fetched, keys = cached
        fresh = fetched + CACHE_SECONDS > now
        # A key we don't know yet may be a rotation: fetch again, but at most once a minute.
        if fresh and (not refresh or fetched + 60 > now):
            return keys
    try:
        answer = httpx.get(uri, timeout=8.0, headers={"Accept": "application/json"})
        answer.raise_for_status()
        keys = answer.json()
    except Exception as exc:  # noqa: BLE001
        raise OidcError("jwks_unavailable", f"OpenFeed could not fetch {config.display_name}'s signing keys.", 503) from exc
    if not isinstance(keys, dict) or not isinstance(keys.get("keys"), list):
        raise OidcError("jwks_invalid", f"{config.display_name}'s signing keys could not be read.", 503)
    with _cache_lock:
        _jwks_cache[uri] = (now, keys)
    return keys


def clear_caches() -> None:
    with _cache_lock:
        _discovery_cache.clear()
        _jwks_cache.clear()


def _signing_key(config: Provider, doc: dict[str, Any], header: dict[str, Any]) -> Any:
    alg = str(header.get("alg") or "")
    if alg in _SYMMETRIC:
        if not config.client_secret:
            raise OidcError("id_token_alg", "The provider signed the ID token with a shared secret, but this client has none.", 403)
        return config.client_secret.encode("utf-8")
    if alg not in _ASYMMETRIC:
        raise OidcError("id_token_alg", "The ID token is unsigned or uses an algorithm OpenFeed does not accept.", 403)
    kid = header.get("kid")
    for refresh in (False, True):
        keys = _jwks(config, doc, refresh=refresh)
        candidates = [k for k in keys["keys"] if isinstance(k, dict) and (kid is None or k.get("kid") == kid)
                      and k.get("use", "sig") == "sig"]
        for candidate in candidates:
            try:
                return jwt.PyJWK.from_dict(candidate, algorithm=alg).key
            except Exception:  # noqa: BLE001 - a key of another type; try the next
                continue
    raise OidcError("id_token_key", "The ID token was signed with a key the provider does not publish.", 403)


def verify_id_token(config: Provider, doc: dict[str, Any], token: str, *, nonce: str) -> dict[str, Any]:
    """The verified claims of an ID token, or OidcError. Every check is here."""
    if not token or token.count(".") != 2:
        raise OidcError("id_token_missing", f"{config.display_name} did not return an ID token.", 403)
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise OidcError("id_token_invalid", "The ID token could not be read.", 403) from exc
    key = _signing_key(config, doc, header)
    try:
        claims = jwt.decode(
            token, key=key, algorithms=[str(header.get("alg"))], audience=config.client_id,
            issuer=expected_issuers(config), leeway=LEEWAY_SECONDS,
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise OidcError("id_token_expired", "The sign-in took too long or the clocks disagree. Try again.", 403) from exc
    except jwt.InvalidAudienceError as exc:
        raise OidcError("id_token_audience", "The ID token was made for a different application.", 403) from exc
    except jwt.InvalidIssuerError as exc:
        raise OidcError("id_token_issuer", "The ID token came from a different issuer than the one configured.", 403) from exc
    except jwt.ImmatureSignatureError as exc:
        raise OidcError("id_token_future", "The ID token is not valid yet. Check the server clocks.", 403) from exc
    except jwt.InvalidSignatureError as exc:
        raise OidcError("id_token_signature", "The ID token's signature does not check out.", 403) from exc
    except jwt.PyJWTError as exc:
        raise OidcError("id_token_invalid", "The ID token could not be verified.", 403) from exc
    audience = claims.get("aud")
    if isinstance(audience, list) and len(audience) > 1 and claims.get("azp") != config.client_id:
        raise OidcError("id_token_audience", "The ID token was made for a different application.", 403)
    if claims.get("azp") and claims.get("azp") != config.client_id:
        raise OidcError("id_token_audience", "The ID token was made for a different application.", 403)
    if not hmac.compare_digest(str(claims.get("nonce") or ""), str(nonce or "")) or not nonce:
        raise OidcError("id_token_nonce", "The ID token does not belong to this sign-in (nonce mismatch).", 403)
    if not str(claims.get("sub") or "").strip():
        raise OidcError("id_token_sub", f"{config.display_name} did not return a subject identifier.", 403)
    return claims


def groups_from(config: Provider, claims: dict[str, Any]) -> list[str]:
    value: Any = claims.get(config.groups_claim)
    if value is None and config.legacy:
        value = claims.get("ak_groups")
    if isinstance(value, dict):  # Zitadel's project roles: {"role": {...}}
        value = list(value.keys())
    if isinstance(value, str):
        value = [part for part in re.split(r"[,\s]+", value) if part]
    if not isinstance(value, list):
        return []
    return sorted({str(item) for item in value if str(item).strip()})[:100]


# --- the flow -------------------------------------------------------------------------------


def _m() -> Any:
    from . import main

    return main


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def pkce_challenge(verifier: str) -> str:
    return _b64(hashlib.sha256(verifier.encode("ascii")).digest())


def _state_hash(state: str) -> str:
    return hashlib.sha256(state.encode("utf-8")).hexdigest()


def callback_url(config: Provider, request: Request | None = None) -> str:
    if config.redirect_uri:
        return config.redirect_uri
    public = get_settings().public_url
    if public:
        return public + config.callback_path
    return str(request.base_url).rstrip("/") + config.callback_path if request is not None else config.callback_path


def _error_page(title: str, message: str, status_code: int = 400, *, actions: list[tuple[str, str, bool]] | None = None) -> Response:
    response = _m()._auth_error_page(title, message, status_code=status_code, actions=actions)
    response.delete_cookie(get_settings().oidc_state_cookie, path="/", secure=signin.cookie_secure(), samesite="lax")
    response.headers["Cache-Control"] = "no-store"
    return response


def _redirect(url: str) -> RedirectResponse:
    response = RedirectResponse(url, status_code=303)
    response.headers["Cache-Control"] = "no-store"
    return response


def start(request: Request, name: str | None, next_path: str = "/", intent: str = "signin", invite: str = "") -> Response:
    """Send the browser to the provider. intent: signin, link or reauth."""
    settings = get_settings()
    intent = intent if intent in {"signin", "link", "reauth"} else "signin"
    nxt = signin.safe_next(next_path, "/account" if intent != "signin" else "/")
    if not settings.sign_in_enabled:
        return _m()._auth_error_page("OpenFeed login is disabled", "Sign-in is turned off on this server, so there is nothing to log in to.",
                                     ["OPENFEED_AUTH_MODE=authentik_oidc"], status_code=404,
                                     fix="To enable Authentik login, add this to the .env file next to compose.yml and run ./deploy.sh. Until then OpenFeed runs as one local profile.")
    config = provider(name)
    if not config:
        if name:
            return _error_page("Unknown sign-in provider", "This server has no sign-in provider by that name.", 404)
        missing = _m()._auth_missing_envs()
        return _m()._auth_error_page("OpenFeed Authentik login is not configured",
                                     "Create or assign an Authentik OIDC provider for OpenFeed, then set these environment variables.", missing)
    account_id = ""
    if intent in {"link", "reauth"}:
        user = _m()._current_user(request)
        if not user.get("session_backed"):
            return _redirect(f"/auth/login?next={urllib.parse.quote(nxt)}")
        if intent == "link" and not signin.reauth_fresh(user):
            return _redirect(f"/auth/confirm?next={urllib.parse.quote(nxt)}")
        account_id = str(user["account_id"])
    invite_hash = ""
    if invite and intent == "signin":
        link = accounts.find_link("invite", signin.token_hash(invite))
        if not link or accounts.registration_mode() == "closed":
            return _error_page("This invite link doesn't work", "It has expired, was already used, or invites are turned off. Ask for a new one.", 410)
        invite_hash = signin.token_hash(invite)
    try:
        doc = discovery(config)
    except OidcError as exc:
        return _error_page(f"{config.display_name} sign-in is unavailable", exc.message, exc.status_code)
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(48)
    redirect_uri = callback_url(config, request)
    authstate.put("oidc", state, {
        "provider": config.name, "nonce": nonce, "verifier": verifier, "next": nxt, "intent": intent,
        "account_id": account_id, "invite": invite_hash, "redirect_uri": redirect_uri, "started": int(time.time()),
    }, FLOW_SECONDS)
    params = {
        "response_type": "code", "client_id": config.client_id, "redirect_uri": redirect_uri, "scope": config.scopes,
        "state": state, "nonce": nonce, "code_challenge": pkce_challenge(verifier), "code_challenge_method": "S256",
    }
    if intent == "reauth":
        # Confirming it's you means signing in at the provider again, not reusing its session.
        params["prompt"] = "login"
        params["max_age"] = "0"
    endpoint = str(doc["authorization_endpoint"])
    joiner = "&" if "?" in endpoint else "?"
    response = _redirect(f"{endpoint}{joiner}{urllib.parse.urlencode(params)}")
    response.set_cookie(
        settings.oidc_state_cookie, signin.sign("oidc-state", {"s": _state_hash(state), "exp": int(time.time()) + FLOW_SECONDS}),
        max_age=FLOW_SECONDS, httponly=True, secure=signin.cookie_secure(), samesite="lax", path="/",
    )
    return response


def _token_request(config: Provider, doc: dict[str, Any], code: str, flow: dict[str, Any]) -> dict[str, Any]:
    data = {"grant_type": "authorization_code", "code": code, "redirect_uri": flow["redirect_uri"], "code_verifier": flow["verifier"]}
    auth: Any = None
    method = config.token_auth
    if not method:
        supported = doc.get("token_endpoint_auth_methods_supported") or ["client_secret_basic"]
        method = "client_secret_basic" if "client_secret_basic" in supported else "client_secret_post"
    if not config.client_secret or method == "none":
        data["client_id"] = config.client_id
    elif method == "client_secret_post":
        data["client_id"] = config.client_id
        data["client_secret"] = config.client_secret
    else:
        auth = (urllib.parse.quote(config.client_id, safe=""), urllib.parse.quote(config.client_secret, safe=""))
    try:
        answer = httpx.post(str(doc["token_endpoint"]), data=data, auth=auth, headers={"Accept": "application/json"}, timeout=10.0)
    except Exception as exc:  # noqa: BLE001
        raise OidcError("token_unavailable", f"OpenFeed could not reach {config.display_name} to finish signing in.", 503) from exc
    if answer.status_code >= 400:
        # A used or expired code (a replayed callback) ends here too.
        logger.info("OIDC token request to %s refused: %s", config.name, answer.status_code)
        raise OidcError("token_refused", f"{config.display_name} refused the sign-in code. It may have been used already or expired. Start again.", 403)
    try:
        payload = answer.json()
    except Exception as exc:  # noqa: BLE001
        raise OidcError("token_invalid", f"{config.display_name} returned an unreadable token response.", 403) from exc
    if not isinstance(payload, dict):
        raise OidcError("token_invalid", f"{config.display_name} returned an unreadable token response.", 403)
    return payload


def _userinfo(config: Provider, doc: dict[str, Any], access_token: str, sub: str) -> dict[str, Any]:
    endpoint = str(doc.get("userinfo_endpoint") or "")
    if not endpoint or not access_token:
        return {}
    try:
        answer = httpx.get(endpoint, headers={"Accept": "application/json", "Authorization": f"Bearer {access_token}"}, timeout=10.0)
        answer.raise_for_status()
        info = answer.json()
    except Exception:  # noqa: BLE001 - the ID token already proved who it is
        logger.info("OIDC userinfo from %s failed; using the ID token's claims", config.name)
        return {}
    if not isinstance(info, dict):
        return {}
    if str(info.get("sub") or "") != sub:
        raise OidcError("userinfo_sub", f"{config.display_name}'s userinfo answer is for a different person than the ID token.", 403)
    return info


def finish(request: Request, name: str | None, code: str | None, state: str | None, error: str | None = None,
           error_description: str | None = None, iss: str | None = None) -> Response:
    settings = get_settings()
    if not settings.sign_in_enabled:
        return _error_page("OpenFeed login is disabled", "Sign-in is not enabled for OpenFeed.", 404)
    if not code and not state and not error:
        response = _redirect("/auth/login?next=/")
        response.delete_cookie(settings.oidc_state_cookie, path="/", secure=signin.cookie_secure(), samesite="lax")
        return response
    if error:
        # The flow is spent either way.
        if state:
            authstate.take("oidc", state)
        detail = re.sub(r"[^\w .,:;'()/-]", "", str(error_description or error).replace("+", " "))[:200]
        return _error_page("Sign-in was cancelled or refused", f"The provider answered: {detail}", 400)
    if not code or not state:
        return _error_page("This sign-in link is incomplete", "The provider did not return the expected code and state. Start again.", 400)
    cookie = signin.unsign("oidc-state", (getattr(request, "cookies", None) or {}).get(settings.oidc_state_cookie))
    # The flow is taken before anything else: a second use of this state (a replayed or
    # double-submitted callback) finds nothing and never reaches the token endpoint.
    flow = authstate.take("oidc", state)
    if not flow or not cookie or not hmac.compare_digest(str(cookie.get("s") or ""), _state_hash(state)):
        return _error_page("This sign-in has expired or was already used",
                           "The sign-in state is missing, expired, already used, or was started in another browser. Start again.", 400)
    if name and flow.get("provider") != name:
        return _error_page("This sign-in went to the wrong place", "The provider in the address does not match the one this sign-in started with.", 400)
    config = provider(str(flow.get("provider") or ""))
    if not config:
        return _error_page("Unknown sign-in provider", "That provider is no longer configured here.", 404)
    if iss and not any(_same_issuer(iss, i) for i in expected_issuers(config)):
        return _error_page(f"{config.display_name} sign-in failed", "The answer came from a different issuer than the one this sign-in went to.", 400)
    try:
        doc = discovery(config)
        tokens = _token_request(config, doc, code, flow)
        claims = verify_id_token(config, doc, str(tokens.get("id_token") or ""), nonce=str(flow.get("nonce") or ""))
        if flow.get("intent") == "reauth" and claims.get("auth_time") and int(claims["auth_time"]) < int(flow.get("started") or 0) - LEEWAY_SECONDS:
            raise OidcError("reauth_stale", f"{config.display_name} did not ask you to sign in again. Try again.", 403)
        info = _userinfo(config, doc, str(tokens.get("access_token") or ""), str(claims["sub"]))
    except OidcError as exc:
        logger.info("OIDC sign-in with %s refused: %s", config.name, exc.code)
        return _error_page(f"{config.display_name} sign-in failed", exc.message, exc.status_code)
    merged = {**claims, **{k: v for k, v in info.items() if k not in {"iss", "aud", "exp", "iat", "nonce"}}}
    return signed_in(request, config, flow, merged)


def _name_from(claims: dict[str, Any]) -> tuple[str, str]:
    email = str(claims.get("email") or "").strip().lower()
    display = str(claims.get("name") or claims.get("preferred_username") or claims.get("nickname") or "").strip()
    username = str(claims.get("preferred_username") or claims.get("nickname") or (email.split("@", 1)[0] if email else "") or display or "user")
    return username, display or username


def _refused(config: Provider) -> Response:
    return _error_page(
        "Sign-in isn't open",
        f"There is no OpenFeed account linked to this {config.display_name} sign-in, and this server doesn't create new "
        "accounts without an invite. If you already have an account, sign in another way and link "
        f"{config.display_name} in Account & security. Otherwise ask an admin for an invite link.",
        403, actions=[("Sign in another way", "/auth/login", True)],
    )


def signed_in(request: Request, config: Provider, flow: dict[str, Any], claims: dict[str, Any]) -> Response:
    """The provider vouched for claims['sub']. Sign in, link it to the current account, or
    confirm it's you."""
    main = _m()
    sub = str(claims["sub"]).strip()
    groups = groups_from(config, claims)
    intent = str(flow.get("intent") or "signin")
    next_path = signin.safe_next(str(flow.get("next") or "/"))
    identity = accounts.find_identity(config.key, sub)
    if intent in {"link", "reauth"}:
        current = main._current_user(request)
        if not current.get("session_backed") or str(current.get("account_id")) != str(flow.get("account_id")):
            return _redirect("/auth/login")
        if intent == "reauth":
            if not identity or str(identity["id"]) != str(current["account_id"]):
                return _redirect("/account?error=oidc_mismatch")
            response = _redirect(next_path)
            signin.rotate(request, current, response, reauth=True)
            return response
        if identity:
            same = str(identity["id"]) == str(current["account_id"])
            return _redirect("/account?done=linked" if same else "/account?error=identity_taken")
        # "Confirm it's you" was checked when the link started (start()); the flow is
        # bound to this account, so a provider round trip that outlasts it still counts.
        try:
            accounts.add_identity(current["account_id"], config.key, sub, config.display_name)
        except accounts.AccountError as exc:
            return _redirect(f"/account?error={exc.code}")
        return _redirect("/account?done=linked")
    # Signing in. The provider's access group first.
    required = config.required_group
    if required and required not in groups and not set(groups) & config.admin_groups:
        return main._access_denied_response({"authenticated": True, "groups": groups})
    username, display = _name_from(claims)
    if config.legacy:
        # Unchanged from before several providers: the users row carries the IdP name.
        try:
            main.db.upsert_user(sub, display, {"username": username, "groups": groups, "source": "authentik"})
        except Exception:  # noqa: BLE001
            logger.warning("could not record the Authentik user row", exc_info=True)
    if identity:
        account = identity
        accounts.touch_identity(identity["identity_id"])
    else:
        account = accounts.get_by_user_key(sub) if config.legacy else None
        if account:
            # An account first made by an Authentik sign-in (its user_key is the sub).
            accounts.add_identity(account["id"], config.key, sub, config.display_name)
        else:
            made = _create_account(request, config, flow, sub, username, display)
            if isinstance(made, Response):
                return made
            account = made
    if account.get("disabled"):
        return _error_page("This OpenFeed account is disabled", "An admin turned this account off. Ask them to turn it back on.",
                           403, actions=[("Return to OpenFeed", "/", True)])
    return signin.complete_first_factor(request, account, config.key, next_path, idp_groups=groups)


def _create_account(request: Request, config: Provider, flow: dict[str, Any], sub: str, username: str, display: str) -> dict[str, Any] | Response:
    """A new account for an unknown identity, if this install allows it. Never matched to
    an existing account by email or username."""
    user_key = sub if config.legacy else None
    identity = (config.key, sub, config.display_name)
    invite = str(flow.get("invite") or "")
    try:
        if config.create_accounts == "always":
            return accounts.create_account(accounts.unique_username(username), display, user_key=user_key, identity=identity)
        if invite:
            return accounts.create_account_with_invite(invite, accounts.unique_username(username), display, identity=identity, user_key=user_key)
        if accounts.registration_mode() == "open":
            per_client = ratelimit.client_key(ratelimit.SIGNUP_CLIENT, request)
            everyone = ratelimit.global_key(ratelimit.SIGNUP_GLOBAL)
            if ratelimit.blocked(per_client, ratelimit.SIGNUP_CLIENT) or ratelimit.blocked(everyone, ratelimit.SIGNUP_GLOBAL):
                return _error_page("Too many new accounts right now", "Try again in an hour.", 429)
            ratelimit.hit(per_client, ratelimit.SIGNUP_CLIENT.window)
            ratelimit.hit(everyone, ratelimit.SIGNUP_GLOBAL.window)
            return accounts.create_account(accounts.unique_username(username), display, user_key=user_key, identity=identity)
    except accounts.AccountError as exc:
        if exc.code == "invite_invalid":
            return _error_page("This invite link doesn't work", exc.message, 410)
        return _error_page(f"{config.display_name} sign-in failed", exc.message, exc.status_code)
    return _refused(config)


# --- routes ---------------------------------------------------------------------------------


@router.get("/auth/oidc/{name}/start")
def oidc_start(request: Request, name: str, next: str = "/", intent: str = "signin", invite: str = "") -> Response:
    return start(request, name, next, intent, invite)


@router.get("/auth/oidc/{name}/callback")
def oidc_callback(request: Request, name: str, code: str | None = None, state: str | None = None, error: str | None = None,
                  error_description: str | None = None, iss: str | None = None) -> Response:
    return finish(request, name, code, state, error, error_description, iss)


def single_redirect_provider() -> Provider | None:
    """The provider /auth/login may send people straight to: the only way to sign in."""
    found = providers()
    return found[0] if len(found) == 1 else None


def provider_hosts() -> list[str]:
    """The hosts of the configured providers' sign-in pages (for the mobile app note)."""
    hosts = []
    for config in providers():
        host = urllib.parse.urlsplit(config.issuer).netloc
        if host and host not in hosts:
            hosts.append(host)
    return hosts
