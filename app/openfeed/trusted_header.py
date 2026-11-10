"""Sign-in by a forward-auth proxy's header (Authentik outpost, Authelia, oauth2-proxy,
Cloudflare Access, ...). docs/accounts.md, "Trusted header".

- `OPENFEED_TRUSTED_HEADER` names the header that carries the signed-in user (for example
  `Remote-User`). `OPENFEED_TRUSTED_HEADER_GROUPS` and `_NAME` name optional group and
  display-name headers.
- The headers are believed only when the connection itself comes from an address in
  `OPENFEED_TRUSTED_PROXIES`. From anywhere else they are removed from the request before
  anything else reads it, so a client cannot sign in as someone by sending the header.
- The header value is the identity's subject under the method `header:<header name>`. The
  first time a name arrives an account is made only when registration is open, or always
  with `OPENFEED_TRUSTED_HEADER_CREATE_ACCOUNTS=always`. A signed-in person can link the
  name the proxy reports to their existing account in Account & security.
- Sessions still come from OpenFeed's server-side store: the header starts one, and the
  session cookie carries the rest of the visit. When the proxy reports someone else, the
  old session ends and a new one starts.
- The proxy does the authentication, including any second factor, so OpenFeed does not
  ask for its own two-step code on a header sign-in.
"""

from __future__ import annotations

import logging
import threading
import time
import urllib.parse
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

from . import accounts, ratelimit, signin
from .config import get_settings

logger = logging.getLogger("openfeed.auth")
router = APIRouter(include_in_schema=False)

SUBJECT_MAX = 256
REFUSED_KEY = "openfeed.header_refused"
_CACHE_SECONDS = 30
# subject -> (account id, until). Spares a database lookup on every request of a visit.
_cache: dict[str, tuple[str, float]] = {}
_cache_lock = threading.Lock()


def _header_names() -> list[str]:
    settings = get_settings()
    return [name.strip().lower() for name in (settings.trusted_header, settings.trusted_header_groups, settings.trusted_header_name) if name.strip()]


def method_key() -> str:
    return f"header:{get_settings().trusted_header.strip().lower()}"


def configured() -> bool:
    settings = get_settings()
    return bool(settings.trusted_header.strip()) and settings.sign_in_enabled and signin.secret_configured()


def peer_trusted(request: Request) -> bool:
    client = getattr(request, "client", None)
    peer = str(getattr(client, "host", "") or "")
    networks = ratelimit._trusted_networks()
    return bool(networks) and ratelimit._is_trusted(peer, networks)


def strip(request: Request) -> None:
    """Remove the proxy headers from this request, so nothing downstream sees them."""
    names = {name.encode("latin-1") for name in _header_names()}
    headers = request.scope.get("headers") or []
    kept = [(k, v) for k, v in headers if k.lower() not in names]
    if len(kept) != len(headers):
        request.scope["headers"] = kept
        request.__dict__.pop("_headers", None)


def _split(value: str) -> list[str]:
    parts = [part.strip() for part in value.replace("|", ",").replace(";", ",").split(",")]
    return sorted({part for part in parts if part})[:100]


class HeaderMethod(signin.SignInMethod):
    def __init__(self) -> None:
        super().__init__(key="header:", kind="header", label="Sign-in proxy")

    @property
    def display(self) -> str:
        label = get_settings().trusted_header_label.strip()
        return label[:1].upper() + label[1:] if label else "Sign-in proxy"

    def available(self) -> bool:
        return configured() and bool(ratelimit._trusted_networks())

    def describe(self, identity: dict[str, Any]) -> dict[str, Any]:
        row = super().describe(identity)
        row["label"] = f"{self.display} ({identity.get('subject')})"
        return row

    def group_policy(self) -> tuple[frozenset[str], str]:
        settings = get_settings()
        return (settings.trusted_header_admin_groups or frozenset(settings.admin_groups)), settings.required_group.strip()

    def reauth_entry(self, identities: list[dict[str, Any]], next_path: str) -> signin.MethodEntry | None:
        if not self.available() or not any(i.get("method") == method_key() for i in identities):
            return None
        return signin.MethodEntry(method_key(), f"Confirm with {self.display}", "/auth/header/confirm", "header")


METHOD = HeaderMethod()


def _sync_registry() -> None:
    """Registered under the configured header's key; nothing when no header is set."""
    for key in [m.key for m in list(signin._REGISTRY.values()) if isinstance(m, HeaderMethod)]:
        if key != method_key() or not get_settings().trusted_header.strip():
            signin.unregister(key)
    if get_settings().trusted_header.strip():
        METHOD.key = method_key()
        if signin._REGISTRY.get(METHOD.key) is not METHOD:
            signin.register(METHOD)


signin.register_loader(_sync_registry)


def _cached_account(subject: str) -> str:
    with _cache_lock:
        found = _cache.get(subject)
    return found[0] if found and found[1] > time.time() else ""


def _remember(subject: str, account_id: str) -> None:
    with _cache_lock:
        if len(_cache) > 5000:
            _cache.clear()
        _cache[subject] = (account_id, time.time() + _CACHE_SECONDS)


def forget(subject: str | None = None) -> None:
    with _cache_lock:
        if subject is None:
            _cache.clear()
        else:
            _cache.pop(subject, None)


def reported(request: Request) -> dict[str, Any] | None:
    """What a trusted proxy says about this request: subject, groups, name. None when the
    request did not come through a trusted proxy or carries no user."""
    if not configured() or not peer_trusted(request):
        return None
    settings = get_settings()
    subject = str(request.headers.get(settings.trusted_header) or "").strip()
    if not subject or len(subject) > SUBJECT_MAX:
        return None
    groups = _split(str(request.headers.get(settings.trusted_header_groups) or "")) if settings.trusted_header_groups else []
    name = str(request.headers.get(settings.trusted_header_name) or "").strip()[:80] if settings.trusted_header_name else ""
    return {"subject": subject, "groups": groups, "name": name}


def _may_create() -> bool:
    if get_settings().trusted_header_create == "always":
        return True
    try:
        return accounts.registration_mode() == "open"
    except Exception:  # noqa: BLE001
        return False


def apply(request: Request) -> None:
    """Run once per request from main.harden_http, before anything reads the user."""
    if not get_settings().trusted_header.strip():
        return
    if not peer_trusted(request):
        strip(request)
        return
    who = reported(request)
    if not who:
        return
    try:
        _sign_in(request, who)
    except Exception:  # noqa: BLE001 - fail closed: the request goes on signed out
        logger.warning("trusted-header sign-in failed", exc_info=True)


def _sign_in(request: Request, who: dict[str, Any]) -> None:
    subject = who["subject"]
    key = method_key()
    current = signin.user_from_request(request)
    if current and _cached_account(subject) == current["account_id"]:
        return
    identity = accounts.find_identity(key, subject)
    if current and identity and str(identity["id"]) == str(current["account_id"]):
        _remember(subject, str(identity["id"]))
        return
    if current and not identity:
        # Signed in another way; the person can link this name in Account & security.
        return
    _admin, required = METHOD.group_policy()
    if required and required not in who["groups"] and not set(who["groups"]) & _admin:
        request.scope[REFUSED_KEY] = "group"
        return
    if identity:
        account = identity
    elif _may_create():
        username = subject.split("@", 1)[0]
        account = accounts.create_account(accounts.unique_username(username), who["name"] or username,
                                          identity=(key, subject, METHOD.display))
        identity = accounts.find_identity(key, subject)
        logger.info("created an account for a new trusted-header user")
    else:
        request.scope[REFUSED_KEY] = "closed"
        return
    if account.get("disabled"):
        request.scope[REFUSED_KEY] = "disabled"
        return
    old = signin.session_token(request)
    if old:
        accounts.delete_session_by_hash(signin.token_hash(old))
    token = signin.new_token()
    settings = get_settings()
    accounts.create_session(
        account["id"], signin.token_hash(token), device=signin.device_label(request.headers.get("user-agent")),
        methods=[key], idp_groups=who["groups"], idle_seconds=settings.session_idle_seconds,
        max_seconds=settings.auth_session_ttl_seconds, reauth=True,
    )
    if identity and identity.get("identity_id"):
        accounts.touch_identity(identity["identity_id"])
    _remember(subject, str(account["id"]))
    signin.adopt_session_cookie(request, token)


def refusal_message(request: Request) -> str:
    reason = request.scope.get(REFUSED_KEY) if hasattr(request, "scope") else None
    label = METHOD.display
    if reason == "closed":
        return (f"{label} signed you in, but there is no OpenFeed account for that name and this server doesn't create "
                "accounts without an invite. Ask an admin, or sign in another way and link it in Account & security.")
    if reason == "group":
        return f"{label} signed you in, but your account is missing the group this server requires."
    if reason == "disabled":
        return "An admin turned this account off. Ask them to turn it back on."
    return ""


def logout_target(request: Request) -> str:
    """Where sign-out goes for a header session: the proxy's own sign-out page, if set."""
    url = get_settings().trusted_header_logout_url.strip()
    if not url or not configured():
        return ""
    user = signin.user_from_request(request)
    if user and any(str(m).startswith("header:") for m in user.get("methods") or []):
        return url
    return ""


def link_offer(request: Request, identities: list[dict[str, Any]]) -> str:
    """A "Link" form for Account & security when the proxy reports a name not linked yet."""
    who = reported(request)
    if not who or not METHOD.available():
        return ""
    if any(i.get("method") == method_key() and i.get("subject") == who["subject"] for i in identities):
        return ""
    from html import escape

    return (
        '<form method="post" action="/auth/header/link" class="acct-inline">'
        f'<button class="btn btn-sm" type="submit">Link {escape(METHOD.display)} ({escape(who["subject"])})</button></form>'
    )


def _redirect(url: str) -> RedirectResponse:
    response = RedirectResponse(url, status_code=303)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/auth/header/link")
def link(request: Request) -> Response:
    user = signin.user_from_request(request)
    if not user:
        return _redirect("/auth/login?next=/account")
    if not signin.reauth_fresh(user):
        return _redirect("/auth/confirm?next=/account")
    who = reported(request)
    if not who:
        return _redirect("/account?error=not_found")
    try:
        accounts.add_identity(user["account_id"], method_key(), who["subject"], METHOD.display)
    except accounts.AccountError as exc:
        return _redirect(f"/account?error={exc.code}")
    forget(who["subject"])
    return _redirect("/account?done=linked")


@router.post("/auth/header/confirm")
async def confirm(request: Request) -> Response:
    """"Confirm it's you" for a header session: the trusted proxy still vouches for a name
    linked to this account, on this request."""
    form = await request.form()
    user = signin.user_from_request(request)
    nxt = signin.safe_next(str(form.get("next") or ""), "/account")
    if not user:
        return _redirect(f"/auth/login?next={urllib.parse.quote(nxt)}")
    who = reported(request)
    identity = accounts.find_identity(method_key(), who["subject"]) if who else None
    if not identity or str(identity["id"]) != str(user["account_id"]):
        return _redirect("/account?error=oidc_mismatch")
    response = _redirect(nxt)
    signin.rotate(request, user, response, reauth=True)
    return response
