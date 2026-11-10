"""Passkeys (WebAuthn): add one in Account & security, sign in with one.

docs/accounts.md, "Passkeys", has the long version. In short:

- The relying party ID and origin come from OPENFEED_PUBLIC_URL. Browsers offer passkeys
  only on a secure context, so an http:// public URL on anything but localhost (a LAN IP,
  say) turns them off, and the pages say why.
- Every ceremony starts with options from the server. The challenge is random, kept on the
  server (authstate, 5 minutes) and taken once by the step that checks the answer.
- Attestation "none": OpenFeed does not ask which make of authenticator you use.
- Stored per passkey (account_identities, method 'passkey', subject = credential id):
  the public key, the signature counter, the WebAuthn user handle, transports and the
  backup flags. Never a private key; a copy of the database cannot sign in.
- Signature counters: when either the stored or the new counter is non-zero, the new one
  must be larger (py_webauthn enforces it). A counter that went backwards means a cloned
  authenticator, and the sign-in is refused and logged. Synced passkeys report 0 always,
  which is fine.
- A passkey sign-in with user verification (a PIN, fingerprint or face) proves both
  "something you have" and "something you are/know", so two-step verification is not
  asked again. Without it, it is one factor, and an account with 2FA gets the code step.
- Usernameless ("Sign in with a passkey", or the browser's autofill on the username field
  where it supports conditional mediation) uses discoverable credentials; typing a
  username first narrows the browser's choice to that account's passkeys.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import secrets
import unicodedata
import urllib.parse
from html import escape
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from . import accounts, authstate, ratelimit, signin
from .config import get_settings

logger = logging.getLogger("openfeed.auth")
router = APIRouter(include_in_schema=False)

METHOD_KEY = "passkey"
CHALLENGE_SECONDS = 5 * 60
CEREMONY_TIMEOUT_MS = 120_000
NAME_MAX = 60
# Options only mint a short-lived, single-use challenge, and every sign-in page load asks
# for one (the browser's passkey autofill), so a household or office behind one address
# must not run out of them. Per client they are generous; what is capped is how many
# challenges the whole install keeps at once (each lives CHALLENGE_SECONDS). The strict
# limits are on the verify step: per account (VERIFY_ACCOUNT) and per client
# (ratelimit.SIGNIN_CLIENT, shared with password sign-in).
OPTIONS_CLIENT = ratelimit.Rule("passkey-options", 600, CHALLENGE_SECONDS)
OPTIONS_GLOBAL = ratelimit.Rule("passkey-options-all", 5000, CHALLENGE_SECONDS)
VERIFY_ACCOUNT = ratelimit.Rule("passkey", 8, 15 * 60)
_SCRIPT = Path(__file__).resolve().parent / "assets" / "auth" / "passkeys.js"


# --- configuration -------------------------------------------------------------------------


def _webauthn() -> Any:
    import webauthn

    return webauthn


def relying_party() -> tuple[str, str]:
    """(RP ID, origin) from OPENFEED_PUBLIC_URL."""
    parts = urllib.parse.urlsplit(get_settings().public_url)
    return (parts.hostname or "").lower(), f"{parts.scheme}://{parts.netloc}".lower()


def status() -> tuple[bool, str]:
    """(passkeys usable on this install, the reason when they are not, for the page)."""
    settings = get_settings()
    mode = settings.passkeys
    if mode in {"off", "false", "0", "no"}:
        return False, "Passkeys are turned off on this server (OPENFEED_PASSKEYS=off)."
    if not settings.sign_in_enabled or not signin.secret_configured():
        return False, ""
    if mode not in {"on", "true", "1", "yes"} and not settings.password_signin_enabled:
        return False, ""
    try:
        _webauthn()
    except ImportError:
        return False, "Passkeys need the webauthn Python package, which is not installed."
    parts = urllib.parse.urlsplit(settings.public_url)
    host = (parts.hostname or "").lower()
    if not host:
        return False, "Passkeys need OPENFEED_PUBLIC_URL, the address people open OpenFeed at."
    try:
        ipaddress.ip_address(host)
        is_ip = True
    except ValueError:
        is_ip = False
    if is_ip:
        return False, (f"Passkeys need a domain name. This server's address ({settings.public_url}) is an IP address, "
                       "and browsers only tie passkeys to names. Use an https address with a name.")
    if parts.scheme != "https" and host != "localhost" and not host.endswith(".localhost"):
        return False, (f"Passkeys need a secure connection. This server's address ({settings.public_url}) is plain http, "
                       "and browsers only offer passkeys on https pages.")
    return True, ""


def available() -> bool:
    return status()[0]


def _b64url(data: bytes) -> str:
    from webauthn.helpers import bytes_to_base64url

    return bytes_to_base64url(data)


def _unb64url(value: str) -> bytes:
    from webauthn.helpers import base64url_to_bytes

    return base64url_to_bytes(value)


def clean_name(value: Any, fallback: str = "Passkey") -> str:
    name = "".join(ch for ch in unicodedata.normalize("NFKC", str(value or "")) if unicodedata.category(ch)[0] != "C")
    name = " ".join(name.split())[:NAME_MAX]
    return name or fallback


# --- the registry entry ---------------------------------------------------------------------


class PasskeyMethod(signin.SignInMethod):
    def __init__(self) -> None:
        super().__init__(key=METHOD_KEY, kind="passkey", label="Passkey", both_factors=True)

    def available(self) -> bool:
        return available()

    def entry(self, next_path: str) -> signin.MethodEntry | None:
        return signin.MethodEntry(METHOD_KEY, "Sign in with a passkey", "", "passkey")

    def describe(self, identity: dict[str, Any]) -> dict[str, Any]:
        row = super().describe(identity)
        row["label"] = identity.get("label") or "Passkey"
        return row

    def reauth_entry(self, identities: list[dict[str, Any]], next_path: str) -> signin.MethodEntry | None:
        if not available() or not any(i.get("method") == METHOD_KEY for i in identities):
            return None
        return signin.MethodEntry(METHOD_KEY, "Confirm with a passkey", "", "passkey")


METHOD = signin.register(PasskeyMethod())


# --- the script ---------------------------------------------------------------------------


def _script_text() -> str:
    try:
        return _SCRIPT.read_text(encoding="utf-8")
    except OSError:
        return ""


def script_tag() -> str:
    digest = hashlib.sha256(_script_text().encode("utf-8")).hexdigest()[:16]
    return f'<script src="/assets/auth/passkeys-{digest}.js" defer></script>'


@router.get("/assets/auth/passkeys-{digest}.js")
def passkeys_script(digest: str) -> Response:
    body = _script_text()
    if digest != hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]:
        raise HTTPException(status_code=404, detail="Not found")
    return Response(body, media_type="text/javascript", headers={"cache-control": "public, max-age=31536000, immutable"})


def page_attrs() -> str:
    """data- attributes the script reads: whether passkeys are on, the origin they belong
    to, and why not when they are off."""
    ok, reason = status()
    _rp, origin = relying_party()
    return (f' data-passkeys="{"on" if ok else "off"}" data-passkey-origin="{escape(origin, quote=True)}"'
            f' data-passkey-reason="{escape(reason, quote=True)}"')


# --- helpers --------------------------------------------------------------------------------


def _json_error(message: str, status_code: int, **extra: Any) -> JSONResponse:
    response = JSONResponse({"error": message, **extra}, status_code=status_code)
    response.headers["Cache-Control"] = "no-store"
    return response


def _as_json(response: Response) -> JSONResponse:
    """A redirect (with its cookies) as JSON, for the script that called fetch()."""
    out = JSONResponse({"redirect": response.headers.get("location") or "/"})
    for key, value in response.raw_headers:
        if key.lower() == b"set-cookie":
            out.raw_headers.append((key, value))
    out.headers["Cache-Control"] = "no-store"
    return out


async def _body(request: Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except (ValueError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _descriptors(identities: list[dict[str, Any]]) -> list[Any]:
    from webauthn.helpers.structs import AuthenticatorTransport, PublicKeyCredentialDescriptor

    out = []
    for identity in identities:
        transports = []
        for value in (identity.get("data") or {}).get("transports") or []:
            try:
                transports.append(AuthenticatorTransport(value))
            except ValueError:
                continue
        try:
            out.append(PublicKeyCredentialDescriptor(id=_unb64url(identity["subject"]), transports=transports or None))
        except Exception:  # noqa: BLE001 - a malformed stored id is skipped
            continue
    return out


def _session(request: Request) -> dict[str, Any] | None:
    user = signin.user_from_request(request)
    return user if user and user.get("session_backed") else None


GENERIC_FAILURE = "That passkey didn't work here. Try again, or sign in another way."


# --- sign in and confirm it's you ---------------------------------------------------------


@router.post("/auth/passkey/options")
async def sign_in_options(request: Request) -> Response:
    ok, reason = status()
    if not ok:
        return _json_error(reason or "Passkeys are not available on this server.", 404)
    body = await _body(request)
    bucket = ratelimit.client_key(OPTIONS_CLIENT, request)
    everyone = ratelimit.global_key(OPTIONS_GLOBAL)
    if ratelimit.blocked(bucket, OPTIONS_CLIENT) or ratelimit.blocked(everyone, OPTIONS_GLOBAL):
        if body.get("conditional") is True:
            # The page-load (autofill) request: nobody asked for it, so a limit is not an
            # error. A 200 keeps the browser console quiet; the script just stops.
            return _quiet()
        return _json_error("Too many passkey requests right now. Wait a few minutes and try again.", 429)
    ratelimit.hit(bucket, OPTIONS_CLIENT.window)
    ratelimit.hit(everyone, OPTIONS_GLOBAL.window)
    intent = "reauth" if body.get("intent") == "reauth" else "signin"
    from webauthn.helpers.structs import UserVerificationRequirement

    allow: list[dict[str, Any]] = []
    account_id = ""
    username = ""
    if intent == "reauth":
        user = _session(request)
        if not user:
            return _json_error("Sign in first.", 401)
        account_id = str(user["account_id"])
        allow = accounts.list_method_identities(account_id, METHOD_KEY)
        if not allow:
            return _json_error("This account has no passkey.", 400)
    else:
        username = unicodedata.normalize("NFKC", str(body.get("username") or "")).strip().lower()[:64]
        if username:
            # An unknown name gets the same answer as a name without passkeys: the
            # browser's own list (discoverable credentials). Nothing tells them apart.
            account = accounts.get_by_username(username)
            if account and not account.get("disabled"):
                allow = accounts.list_method_identities(account["id"], METHOD_KEY)
    rp_id, _origin = relying_party()
    options = _webauthn().generate_authentication_options(
        rp_id=rp_id, timeout=CEREMONY_TIMEOUT_MS, allow_credentials=_descriptors(allow),
        user_verification=UserVerificationRequirement.REQUIRED if intent == "reauth" else UserVerificationRequirement.PREFERRED,
    )
    flow = secrets.token_urlsafe(24)
    authstate.put("passkey", flow, {
        "challenge": _b64url(options.challenge), "intent": intent, "account_id": account_id, "username": username,
        "next": signin.safe_next(str(body.get("next") or ""), "/account" if intent == "reauth" else "/"),
        "uv": intent == "reauth",
    }, CHALLENGE_SECONDS)
    from webauthn.helpers import options_to_json_dict

    response = JSONResponse({"flow": flow, "publicKey": options_to_json_dict(options)})
    response.headers["Cache-Control"] = "no-store"
    return response


def _quiet() -> JSONResponse:
    response = JSONResponse({"quiet": True})
    response.headers["Cache-Control"] = "no-store"
    return response


def _fail(request: Request, message: str = GENERIC_FAILURE, status_code: int = 401, *, account_id: Any = None) -> JSONResponse:
    """A refused answer: counts against this client and, when the passkey named an
    account, against that account."""
    ratelimit.hit(ratelimit.client_key(ratelimit.SIGNIN_CLIENT, request), ratelimit.SIGNIN_CLIENT.window)
    if account_id:
        ratelimit.hit(ratelimit.account_key(VERIFY_ACCOUNT, account_id), VERIFY_ACCOUNT.window)
    return _json_error(message, status_code)


@router.post("/auth/passkey/verify")
async def sign_in_verify(request: Request) -> Response:
    if not available():
        return _json_error("Passkeys are not available on this server.", 404)
    if ratelimit.blocked(ratelimit.client_key(ratelimit.SIGNIN_CLIENT, request), ratelimit.SIGNIN_CLIENT):
        return _json_error("Too many sign-in attempts. Wait 15 minutes and try again.", 429)
    body = await _body(request)
    credential = body.get("credential")
    flow = authstate.take("passkey", str(body.get("flow") or ""))
    if not flow:
        return _fail(request, "This passkey request expired or was already used. Try again.", 400)
    if not isinstance(credential, dict) or not isinstance(credential.get("id"), str):
        return _fail(request)
    identity = accounts.find_identity(METHOD_KEY, credential["id"])
    if not identity or identity.get("disabled"):
        return _fail(request)
    account_id = identity["id"]
    if ratelimit.blocked(ratelimit.account_key(VERIFY_ACCOUNT, account_id), VERIFY_ACCOUNT):
        return _json_error("Too many sign-in attempts. Wait 15 minutes and try again.", 429)
    if flow.get("username") and str(identity.get("username") or "").lower() != flow["username"]:
        return _fail(request, account_id=account_id)
    current = None
    if flow.get("intent") == "reauth":
        current = _session(request)
        if not current or str(current["account_id"]) != str(flow.get("account_id")) or str(identity["id"]) != str(current["account_id"]):
            return _fail(request, account_id=account_id)
    data = identity.get("data") or {}
    handle = ((credential.get("response") or {}) if isinstance(credential.get("response"), dict) else {}).get("userHandle")
    if handle and data.get("user_handle") and handle != data["user_handle"]:
        return _fail(request, account_id=account_id)
    rp_id, origin = relying_party()
    try:
        verified = _webauthn().verify_authentication_response(
            credential=credential, expected_challenge=_unb64url(flow["challenge"]), expected_rp_id=rp_id,
            expected_origin=origin, credential_public_key=_unb64url(str(data.get("public_key") or "")),
            credential_current_sign_count=int(data.get("sign_count") or 0), require_user_verification=bool(flow.get("uv")),
        )
    except Exception as exc:  # noqa: BLE001 - every library refusal is a failed sign-in
        if "sign count" in str(exc).lower():
            logger.warning("passkey refused: its signature counter went backwards (a cloned authenticator?) for account %s", identity["id"])
        else:
            logger.info("passkey refused: %s", type(exc).__name__)
        return _fail(request, account_id=account_id)
    ratelimit.clear(ratelimit.account_key(VERIFY_ACCOUNT, account_id))
    accounts.update_identity_data(identity["identity_id"], {
        "sign_count": int(verified.new_sign_count), "backed_up": bool(verified.credential_backed_up),
    })
    if current is not None:
        response = JSONResponse({"redirect": flow.get("next") or "/account"})
        response.headers["Cache-Control"] = "no-store"
        signin.rotate(request, current, response, reauth=True)
        return response
    redirect = signin.complete_first_factor(request, identity, METHOD_KEY, str(flow.get("next") or "/"),
                                            both_factors=bool(verified.user_verified))
    return _as_json(redirect)


# --- adding, renaming --------------------------------------------------------------------------


@router.post("/account/passkeys/options")
async def registration_options(request: Request) -> Response:
    ok, reason = status()
    if not ok:
        return _json_error(reason or "Passkeys are not available on this server.", 404)
    user = _session(request)
    if not user:
        return _json_error("Sign in first.", 401)
    if not signin.reauth_fresh(user):
        return _json_error("Confirm it's you first.", 403, redirect="/auth/confirm?next=/account")
    from webauthn.helpers import generate_user_handle, options_to_json_dict
    from webauthn.helpers.structs import (
        AttestationConveyancePreference,
        AuthenticatorSelectionCriteria,
        ResidentKeyRequirement,
        UserVerificationRequirement,
    )

    body = await _body(request)
    account = accounts.get_account(user["account_id"])
    if not account:
        return _json_error("Sign in first.", 401)
    existing = accounts.list_method_identities(account["id"], METHOD_KEY)
    # One WebAuthn user handle per account, random and unrelated to the user_key, so an
    # authenticator replaces rather than duplicates this account's passkey.
    handle = next((str((i.get("data") or {}).get("user_handle")) for i in existing if (i.get("data") or {}).get("user_handle")), "")
    handle_bytes = _unb64url(handle) if handle else generate_user_handle()
    rp_id, _origin = relying_party()
    options = _webauthn().generate_registration_options(
        rp_id=rp_id, rp_name="OpenFeed", user_id=handle_bytes, user_name=account["username"],
        user_display_name=account.get("display_name") or account["username"], timeout=CEREMONY_TIMEOUT_MS,
        attestation=AttestationConveyancePreference.NONE,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED, user_verification=UserVerificationRequirement.PREFERRED),
        exclude_credentials=_descriptors(existing),
    )
    flow = secrets.token_urlsafe(24)
    authstate.put("passkey-add", flow, {
        "challenge": _b64url(options.challenge), "account_id": account["id"], "user_handle": _b64url(handle_bytes),
        "name": clean_name(body.get("name"), ""),
    }, CHALLENGE_SECONDS)
    response = JSONResponse({"flow": flow, "publicKey": options_to_json_dict(options)})
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/account/passkeys/register")
async def register_passkey(request: Request) -> Response:
    if not available():
        return _json_error("Passkeys are not available on this server.", 404)
    user = _session(request)
    if not user:
        return _json_error("Sign in first.", 401)
    body = await _body(request)
    flow = authstate.take("passkey-add", str(body.get("flow") or ""))
    if not flow or str(flow.get("account_id")) != str(user["account_id"]):
        return _json_error("This passkey request expired or was already used. Try again.", 400)
    credential = body.get("credential")
    if not isinstance(credential, dict):
        return _json_error("The browser sent no passkey.", 400)
    rp_id, origin = relying_party()
    try:
        verified = _webauthn().verify_registration_response(
            credential=credential, expected_challenge=_unb64url(flow["challenge"]), expected_rp_id=rp_id, expected_origin=origin,
        )
    except Exception as exc:  # noqa: BLE001
        logger.info("passkey registration refused: %s", type(exc).__name__)
        return _json_error("The browser's answer did not check out. Nothing was added.", 400)
    response_part = credential.get("response") if isinstance(credential.get("response"), dict) else {}
    transports = [str(t) for t in (response_part.get("transports") or []) if isinstance(t, str)][:8]
    name = clean_name(body.get("name") or flow.get("name"), f"Passkey on {signin.device_label(request.headers.get('user-agent'))}")
    data = {
        "public_key": _b64url(verified.credential_public_key), "sign_count": int(verified.sign_count),
        "user_handle": flow["user_handle"], "transports": transports, "aaguid": str(verified.aaguid),
        "device_type": str(getattr(verified.credential_device_type, "value", verified.credential_device_type)),
        "backed_up": bool(verified.credential_backed_up), "user_verified": bool(verified.user_verified),
    }
    try:
        accounts.add_identity(user["account_id"], METHOD_KEY, _b64url(verified.credential_id), name, data)
    except accounts.AccountError as exc:
        return _json_error("That passkey is already registered." if exc.code == "identity_taken" else exc.message, exc.status_code)
    out = JSONResponse({"redirect": "/account?done=passkey_added"})
    out.headers["Cache-Control"] = "no-store"
    return out


@router.post("/account/passkeys/{identity_id}/rename")
async def rename_passkey(request: Request, identity_id: str) -> Response:
    user = _session(request)
    if not user:
        return RedirectResponse("/auth/login?next=/account", status_code=303)
    form = await request.form()
    name = clean_name(form.get("name"), "")
    if not name:
        return RedirectResponse("/account?error=passkey_name", status_code=303)
    mine = {i["id"] for i in accounts.list_method_identities(user["account_id"], METHOD_KEY)}
    if identity_id not in mine or not accounts.rename_identity(user["account_id"], identity_id, name):
        return RedirectResponse("/account?error=not_found", status_code=303)
    return RedirectResponse("/account?done=passkey_renamed", status_code=303)


# --- Account & security -----------------------------------------------------------------


def account_group(user: dict[str, Any], identities: list[dict[str, Any]], *, rel: Any) -> str:
    """The Passkeys group: each passkey with rename and remove, and "Add a passkey"."""
    from . import account_pages, main

    ok, reason = status()
    passkeys = [i for i in identities if i.get("method") == METHOD_KEY]
    if not ok and not passkeys:
        if not reason:
            return ""
        return main._set_group("Passkeys", main._set_row("Not available here", reason))
    rows = []
    for item in passkeys:
        iid = escape(str(item["id"]), quote=True)
        label = str(item.get("label") or "Passkey")
        note = f"Added {rel(item.get('created_at'))}"
        note += f" · Last used {rel(item.get('last_used_at'))}" if item.get("last_used_at") else " · Not used yet"
        rename = (
            f'<details class="acct-manage pk-rename"><summary class="btn btn-sm" aria-label="Rename {escape(label, quote=True)}">Rename</summary>'
            f'<form class="acct-manage-panel acct-inline" method="post" action="/account/passkeys/{iid}/rename">'
            f'<input class="field-input acct-inline-input" name="name" maxlength="{NAME_MAX}" required value="{escape(label, quote=True)}" '
            f'aria-label="New name for {escape(label, quote=True)}" autocomplete="off">'
            '<button class="btn btn-sm" type="submit">Save</button></form></details>'
        )
        remove = ""
        if signin.can_remove(identities, str(item["id"])):
            remove = (
                f'<form method="post" action="/account/methods/{iid}/remove" data-confirm-title="Remove {escape(label, quote=True)}?" '
                'data-confirm-body="This passkey will no longer sign you in. Remove it from the device too, in its password manager." '
                f'data-confirm-label="Remove"><button class="btn btn-sm btn-danger" type="submit" aria-label="Remove {escape(label, quote=True)}">Remove</button></form>'
            )
        else:
            remove = '<span class="acct-badge" title="Add another way to sign in before removing this one">Only way in</span>'
        rows.append(
            f'<div class="set-row acct-row pk-row" data-passkey-row="{iid}"><span class="acct-icon">{account_pages.icon("key")}</span>'
            f'<div class="set-copy"><strong>{escape(label)}</strong><small>{escape(note)}</small></div>'
            f'<div class="set-control pk-actions">{rename}{remove}</div></div>'
        )
    if not ok:
        rows.append(main._set_row("Can't add or use passkeys here", reason))
        return main._set_group("Passkeys", "".join(rows))
    add = (
        # Without JavaScript the form only reloads the page with a note saying it needs it.
        '<form class="acct-form pk-add" method="get" action="/account" data-passkey-add>'
        '<input type="hidden" name="error" value="passkey_js">'
        '<div class="set-row"><div class="set-copy"><strong>Add a passkey</strong>'
        "<small>Sign in with your fingerprint, face, screen lock or a security key instead of a password. "
        "It counts as both steps of two-step verification.</small></div>"
        '<div class="set-control acct-inline">'
        f'<input class="field-input acct-inline-input" name="name" maxlength="{NAME_MAX}" placeholder="Name, e.g. Phone" '
        'aria-label="Name for the new passkey" autocomplete="off">'
        f'<button class="btn btn-sm" type="submit" data-passkey-add-button>{account_pages.icon("plus")}<span>Add a passkey</span></button>'
        "</div></div>"
        '<p class="acct-form-note pk-note" data-passkey-note role="status" hidden></p>'
        "</form>"
    )
    empty = "" if passkeys else main._set_row("No passkeys yet", "A passkey lives on your phone, computer or security key. The server keeps only its public key.")
    return f'<div class="pk-scope"{page_attrs()}>' + main._set_group("Passkeys", empty + "".join(rows) + add) + "</div>" + script_tag()
