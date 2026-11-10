"""Pages and form handlers for accounts: sign-in, two-step verification, first-run setup,
sign-up and invites, Account & security, and the admin's Users page.

Everything here is a plain HTML form that posts and redirects, so it works without
JavaScript and inside the Capacitor Android WebView. assets/js/accounts.js only adds the
shared confirm dialog and copy buttons on top.

State-changing requests are protected three ways (docs/accounts.md):
- signin.csrf_failure, run in main.harden_http, refuses a POST that cannot show it came
  from this site (Origin, or Referer when a browser drops Origin);
- the session cookie is SameSite=Lax and HttpOnly;
- sensitive changes ask for the password again when the last confirmation is more than
  OPENFEED_REAUTH_SECONDS old (signin.reauth_fresh).
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import urllib.parse
from html import escape
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from . import accounts, passwords, ratelimit, signin, twofactor
from .config import get_settings

logger = logging.getLogger("openfeed.auth")
router = APIRouter(include_in_schema=False)

INVITE_DAYS = (1, 7, 30)
RESET_LINK_SECONDS = 24 * 3600
_REPLACE_SCRIPT = Path(__file__).resolve().parent / "assets" / "auth" / "replace-url.js"

# Icons this module needs that the shared table does not have (Lucide v1.26.0, ISC).
_EXTRA_ICONS = {
    "shield": '<path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/>',
    "key": '<path d="M2.586 17.414A2 2 0 0 0 2 18.828V21a1 1 0 0 0 1 1h3a1 1 0 0 0 1-1v-1a1 1 0 0 1 1-1h1a1 1 0 0 0 1-1v-1a1 1 0 0 1 1-1h.172a2 2 0 0 0 1.414-.586l.814-.814a6.5 6.5 0 1 0-4-4z"/><circle cx="16.5" cy="7.5" r=".5" fill="currentColor"/>',
    "users": '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>',
    "log-out": '<path d="m16 17 5-5-5-5"/><path d="M21 12H9"/><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/>',
}

# Fixed sentences for ?done= and ?error= after a redirect. Only these keys are ever shown,
# so a crafted query string cannot put words on the page.
NOTICES = {
    "name": "Display name saved.",
    "password": "Password changed. Your other devices were signed out.",
    "password_set": "Password set. You can now sign in with your username and password.",
    "2fa_on": "Two-step verification is on.",
    "2fa_off": "Two-step verification is off.",
    "codes": "New recovery codes made. The old ones no longer work.",
    "session": "That device was signed out.",
    "others": "Signed out everywhere else.",
    "method_removed": "Sign-in method removed.",
    "linked": "Sign-in method linked.",
    "confirmed": "Confirmed. Security settings stay unlocked for a few minutes.",
    "created": "Account created. Its set-password link was shown once; make a reset link if it got lost.",
    "role": "Role changed. That person's sessions were ended, so the change applies at their next sign-in.",
    "disabled": "Account disabled and signed out everywhere.",
    "enabled": "Account enabled.",
    "deleted": "Account and all of its data deleted.",
    "reset_2fa": "Two-step verification turned off for that account.",
    "signed_out_user": "That account was signed out everywhere.",
    "registration": "Registration setting saved.",
    "invite_revoked": "Invite link revoked.",
    "kept": "The data from before sign-in is now in your account.",
    "fresh": "Starting fresh. The data from before sign-in stays where it was, unused.",
    "passkey_added": "Passkey added. You can sign in with it from now on.",
    "passkey_renamed": "Passkey renamed.",
    "reset_link": "Password reset link made. It was shown once; make a new one if it got lost.",
    "invite": "Invite link made. It was shown once; make a new one if it got lost.",
}
# Notices the sign-in page shows: only what can lead there.
SIGN_IN_NOTICES = ("password_set",)
ERRORS = {
    "reauth": "Confirm it's you first.",
    "last_admin": "This is the only admin. Make someone else an admin first.",
    "last_method": "That is your only way to sign in. Add another before removing it.",
    "confirm_name": "Type the username exactly to confirm.",
    "identity_taken": "That sign-in is already linked to another account.",
    "not_found": "That item no longer exists.",
    "self": "Use Account & security to change your own account.",
    "oidc_mismatch": "That was a different account at the identity provider.",
    "passkey_name": "Give the passkey a name.",
    "passkey_js": "Adding a passkey needs JavaScript, which is off or blocked in this browser.",
    "invite_invalid": "This invite link has expired or was already used. Ask for a new one.",
}


def _m() -> Any:
    from . import main

    return main


def icon(name: str) -> str:
    main = _m()
    if name in _EXTRA_ICONS:
        main.ICON_PATHS.setdefault(name, _EXTRA_ICONS[name])
    return main._icon(name)


def _q(value: str) -> str:
    return urllib.parse.quote(value, safe="/")


def _redirect(url: str) -> RedirectResponse:
    response = RedirectResponse(url, status_code=303)
    response.headers["Cache-Control"] = "no-store"
    return response


def _no_store(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return response


async def _form(request: Request) -> dict[str, str]:
    form = await request.form()
    return {key: str(value) for key, value in form.items() if isinstance(value, str)}


def _rel(value: Any) -> str:
    return _m()._relative_time(value) or "never"


def _until(value: Any) -> str:
    from datetime import datetime, timezone

    if not isinstance(value, datetime):
        return ""
    when = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    seconds = int((when - datetime.now(timezone.utc)).total_seconds())
    if seconds <= 0:
        return "expired"
    if seconds >= 86400:
        days = seconds // 86400
        return f"expires in {days} day{'s' if days != 1 else ''}"
    hours = max(1, seconds // 3600)
    return f"expires in {hours} hour{'s' if hours != 1 else ''}"


def _notice(request: Request, *, only: tuple[str, ...] | None = None) -> str:
    key = str(request.query_params.get("done") or "")
    done = NOTICES.get(key) if only is None or key in only else None
    error = ERRORS.get(str(request.query_params.get("error") or "")) if only is None else None
    out = ""
    if done:
        out += f'<p class="acct-notice" role="status">{icon("check")}<span>{escape(done)}</span></p>'
    if error:
        out += f'<p class="acct-notice is-error" role="alert">{icon("info")}<span>{escape(error)}</span></p>'
    return out


def _error(message: str) -> str:
    return f'<p class="acct-notice is-error" role="alert">{icon("info")}<span>{escape(message)}</span></p>' if message else ""


def _field(label: str, name: str, *, type_: str = "text", value: str = "", autocomplete: str = "", required: bool = True,
           extra: str = "", hint: str = "", field_id: str = "", input_cls: str = "", error: str = "") -> str:
    """A labelled input. error: a refusal shown right under it (aria-invalid, focused)."""
    fid = field_id or f"f-{name}"
    hint_html = f'<small class="acct-hint" id="{fid}-hint">{escape(hint)}</small>' if hint else ""
    error_html = (f'<small class="acct-field-error" id="{fid}-error" role="alert">{icon("info")}<span>{escape(error)}</span></small>'
                  if error else "")
    described_ids = " ".join(item for item in (f"{fid}-error" if error else "", f"{fid}-hint" if hint else "") if item)
    described = f' aria-describedby="{described_ids}"' if described_ids else ""
    invalid = ' aria-invalid="true" autofocus' if error else ""
    auto = f' autocomplete="{autocomplete}"' if autocomplete else ""
    return (
        f'<label class="text-field acct-field{" has-error" if error else ""}" for="{fid}"><span class="set-label">{escape(label)}</span>'
        f'<input class="field-input{(" " + input_cls) if input_cls else ""}" id="{fid}" name="{name}" type="{type_}" value="{escape(value, quote=True)}"{auto}'
        f'{" required" if required else ""}{described}{invalid} {extra}>{error_html}{hint_html}</label>'
    )


def _username_field(value: str = "", autofocus: bool = True) -> str:
    return _field("Username", "username", value=value, autocomplete="username",
                  extra=f'autocapitalize="none" autocorrect="off" spellcheck="false" maxlength="64"{" autofocus" if autofocus else ""}')


def _new_password_fields(prefix: str = "", error: str = "") -> str:
    return (
        _field("New password" if prefix else "Password", "password", type_="password", autocomplete="new-password",
               extra=f'minlength="{passwords.MIN_LENGTH}" maxlength="{passwords.MAX_LENGTH}"',
               hint=f"At least {passwords.MIN_LENGTH} characters. A few unrelated words work well.", error=error)
        + _field("Repeat it", "password2", type_="password", autocomplete="new-password",
                 extra=f'minlength="{passwords.MIN_LENGTH}" maxlength="{passwords.MAX_LENGTH}"')
    )


def _post(action: str, inner: str, *, cls: str = "", confirm: dict[str, str] | None = None) -> str:
    attrs = ""
    if confirm:
        attrs = "".join(f' data-confirm-{key}="{escape(value, quote=True)}"' for key, value in confirm.items())
    return f'<form method="post" action="{escape(action, quote=True)}" class="{escape(cls, quote=True)}"{attrs}>{inner}</form>'


# --- standalone frames (signed out) ------------------------------------------------------


def _replace_script_text() -> str:
    try:
        return _REPLACE_SCRIPT.read_text(encoding="utf-8")
    except OSError:
        return ""


@router.get("/assets/auth/replace-{digest}.js")
def replace_url_script(digest: str) -> Response:
    body = _replace_script_text()
    if digest != hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]:
        return Response("Not found", status_code=404, media_type="text/plain")
    return Response(body, media_type="text/javascript", headers={"cache-control": "public, max-age=31536000, immutable"})


def replace_url_tag(url: str) -> str:
    """For a page drawn as the answer to a POST: put the matching GET address in the address
    bar (assets/auth/replace-url.js), so Reload does not send the form again. Only a local
    path, and never one holding a secret the page shows."""
    if not url or not url.startswith("/") or url.startswith("//"):
        return ""
    digest = hashlib.sha256(_replace_script_text().encode("utf-8")).hexdigest()[:16]
    return (f'<span hidden data-replace-url="{escape(url, quote=True)}"></span>'
            f'<script src="/assets/auth/replace-{digest}.js"></script>')


def _auth_page(title: str, body: str, *, status_code: int = 200, replace_url: str = "") -> HTMLResponse:
    """The signed-out frame: brand, one card, the app's own stylesheet and theme.

    replace_url: the GET address this page belongs at, when it answers a POST."""
    main = _m()
    html = (
        f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{escape(title)} - OpenFeed</title>'
        f'{main._shell_head()}</head>'
        f'<body class="auth-body"><main class="auth-shell" id="main">'
        f'<a class="brand auth-brand" href="/" aria-label="OpenFeed">{main._brand_mark()}<span class="brand-word">Open<b>Feed</b></span></a>'
        f'<section class="auth-card" aria-labelledby="auth-title">{body}</section>'
        f"</main>{replace_url_tag(replace_url)}</body></html>"
    )
    return _no_store(HTMLResponse(html, status_code=status_code))  # type: ignore[return-value]


def _auth_head(title: str, lede: str = "") -> str:
    lede_html = f'<p class="auth-lede">{escape(lede)}</p>' if lede else ""
    return f'<h1 id="auth-title">{escape(title)}</h1>{lede_html}'


def _app_page(request: Request, title: str, content: str, *, replace_url: str = "") -> HTMLResponse:
    """A page in the app shell. replace_url: the GET address it belongs at, when it answers
    a POST (see replace_url_tag)."""
    html = _m()._app_shell(title, "account", f'<div class="acct-page">{content}</div>', request=request,
                           scripts=replace_url_tag(replace_url))
    return _no_store(HTMLResponse(html))  # type: ignore[return-value]


# --- who is asking -----------------------------------------------------------------------


def _session_user(request: Request) -> dict[str, Any] | None:
    user = _m()._current_user(request)
    return user if user.get("session_backed") else None


def _signed_in_or_redirect(request: Request) -> tuple[dict[str, Any] | None, Response | None]:
    settings = get_settings()
    if not settings.sign_in_enabled:
        return None, _redirect("/?settings=account")
    user = _session_user(request)
    if not user:
        path = request.url.path + (("?" + request.url.query) if request.url.query else "")
        return None, _redirect(f"/auth/login?next={_q(signin.safe_next(path))}")
    return user, None


def _sign_in_off(request: Request) -> Response | None:
    """With sign-in off there is nothing to sign in to, sign up for or reset: the sign-in
    steps go to where the person was headed, or Home. Never to OPENFEED_PUBLIC_URL."""
    if get_settings().sign_in_enabled:
        return None
    return _redirect(signin.safe_next(request.query_params.get("next")))


def _reauth_redirect(back: str) -> RedirectResponse:
    return _redirect(f"/auth/confirm?next={_q(signin.safe_next(back, '/account'))}")


def _account_of(user: dict[str, Any]) -> dict[str, Any]:
    account = accounts.get_account(user["account_id"])
    if not account:
        raise LookupError("account vanished")
    return account


def _is_admin(user: dict[str, Any] | None) -> bool:
    return bool(user and user.get("session_backed") and signin.is_admin(user))


# --- sign-in -----------------------------------------------------------------------------


def _entry_button(entry: signin.MethodEntry, next_path: str, *, confirm: bool = False) -> str:
    """One method on the sign-in or "Confirm it's you" page.

    A passkey is a button the passkey script runs (hidden until it knows this browser can);
    a trusted proxy confirms with a small form; everything else is a plain link, because
    the page's CSP has form-action 'self' and a provider is another site.
    """
    key = escape(entry.key, quote=True)
    if entry.kind == "passkey":
        attr = "data-passkey-confirm" if confirm else "data-passkey-signin"
        return (f'<button class="btn auth-method" type="button" {attr} data-next="{escape(next_path, quote=True)}" '
                f'data-signin-method="{key}" hidden>{icon("key")}<span>{escape(entry.label)}</span></button>')
    if entry.kind == "header":
        return _post(entry.href, f'<input type="hidden" name="next" value="{escape(next_path, quote=True)}">'
                     f'<button class="btn auth-method" type="submit" data-signin-method="{key}">{icon("shield")}<span>{escape(entry.label)}</span></button>',
                     cls="auth-method-form")
    return (f'<a class="btn auth-method" href="{escape(entry.href, quote=True)}" data-signin-method="{key}">'
            f'{icon("external")}<span>{escape(entry.label)}</span></a>')


def _method_buttons(next_path: str) -> str:
    rows = []
    for item in signin.available_methods():
        if item.kind == "password":
            continue
        entry = item.entry(next_path)
        if entry:
            rows.append(_entry_button(entry, next_path))
    if not rows:
        return ""
    note = '<p class="auth-note" data-passkey-note role="status" hidden></p>' if any("data-passkey-signin" in r for r in rows) else ""
    return f'<div class="auth-methods" data-signin-methods>{"".join(rows)}</div>{note}'


def sign_in_page_needed() -> bool:
    """Whether /auth/login shows the sign-in page. Not when one OIDC provider is the only
    way in: existing Authentik installs keep going straight to it."""
    settings = get_settings()
    if not settings.sign_in_enabled:
        return False
    if settings.password_signin_enabled:
        return True
    shown = [m for m in signin.available_methods() if m.kind != "password" and m.entry("/")]
    return not (len(shown) == 1 and shown[0].kind == "oidc")


def sign_in_page(request: Request, next_path: str = "/", *, error: str = "", username: str = "", status_code: int = 200) -> Response:
    settings = get_settings()
    nxt = signin.safe_next(next_path)
    if settings.auth_mode == "accounts" and settings.password_signin_enabled:
        try:
            if not accounts.admin_exists():
                return _redirect("/setup")
        except Exception:  # noqa: BLE001
            logger.warning("could not check for an admin account", exc_info=True)
    if _session_user(request):
        return _redirect(nxt)
    if not signin.secret_configured():
        return _m()._auth_error_page("OpenFeed sign-in is not configured", "Sign-in needs a secret to sign its cookies.",
                                     ["OPENFEED_SECRET_KEY"], status_code=503)
    password_on = settings.password_signin_enabled
    buttons = _method_buttons(nxt)
    passkeys_on = "data-passkey-signin" in buttons
    form = ""
    if password_on:
        user_field = _username_field(username)
        if passkeys_on:
            # Lets the browser offer this site's passkeys in the username autofill.
            user_field = user_field.replace('autocomplete="username"', 'autocomplete="username webauthn"', 1)
        form = (
            f'<form class="auth-form" method="post" action="/auth/login" data-signin-form>'
            f'<input type="hidden" name="next" value="{escape(nxt, quote=True)}">'
            f'{user_field}'
            + _field("Password", "password", type_="password", autocomplete="current-password", extra=f'maxlength="{passwords.MAX_LENGTH}"')
            + '<button class="btn btn-primary auth-submit" type="submit">Sign in</button></form>'
        )
    divider = '<div class="auth-divider" role="separator"><span>or</span></div>' if form and buttons else ""
    foot = []
    if password_on:
        try:
            mode = accounts.registration_mode()
        except Exception:  # noqa: BLE001
            mode = "closed"
        if mode == "open":
            foot.append(f'<a href="/auth/signup?next={_q(nxt)}">Create an account</a>')
        foot.append("<span>Forgot your password? Ask an admin for a reset link, or use a recovery code after your password.</span>")
    if not form and not buttons:
        body = _auth_head("Sign-in is not set up", "No sign-in method is turned on. Set OPENFEED_PASSWORD_SIGNIN=true or configure OPENFEED_OIDC_* in .env.")
        return _auth_page("Sign in", body, status_code=503)
    from . import oidc, passkeys, trusted_header

    error = error or trusted_header.refusal_message(request)
    if "OpenFeedApp" in str(request.headers.get("user-agent") or "") and "data-signin-method=\"oidc:" in buttons:
        hosts = ", ".join(oidc.provider_hosts())
        foot.append(f"<span>In the OpenFeed app, a provider's page opens inside the app only if its host ({escape(hosts)}) "
                    "is listed under App settings, Sign-in hosts.</span>")
    body = (
        _auth_head("Sign in to OpenFeed")
        + (_error(error) or _notice(request, only=SIGN_IN_NOTICES))
        + form + divider + buttons
        + (f'<p class="auth-foot">{" ".join(foot)}</p>' if foot else "")
    )
    if passkeys_on:
        body = f'<div class="auth-passkeys"{passkeys.page_attrs()}>{body}</div>{passkeys.script_tag()}'
    replace = ("/auth/login" + (f"?next={_q(nxt)}" if nxt != "/" else "")) if request.method == "POST" else ""
    return _auth_page("Sign in", body, status_code=status_code, replace_url=replace)


GENERIC_SIGNIN_ERROR = "That username and password don't match an active account."
TOO_MANY = "Too many sign-in attempts. Wait 15 minutes and try again."


def _user_bucket(username: str) -> str:
    # Keyed by the typed name, not the account, so a name nobody has locks out the same way
    # a real one does and the lockout cannot be used to discover accounts.
    return ratelimit.account_key(ratelimit.SIGNIN_ACCOUNT, "u" + ratelimit.client_hash("user:" + username.strip().lower()))


@router.post("/auth/login")
async def sign_in_submit(request: Request) -> Response:
    off = _sign_in_off(request)
    if off:
        return off
    settings = get_settings()
    form = await _form(request)
    nxt = signin.safe_next(form.get("next"))
    username = form.get("username", "")[:64]
    password = form.get("password", "")
    if not settings.password_signin_enabled:
        return sign_in_page(request, nxt, status_code=404)
    client_key = ratelimit.client_key(ratelimit.SIGNIN_CLIENT, request)
    user_key = _user_bucket(username)
    if ratelimit.blocked(client_key, ratelimit.SIGNIN_CLIENT) or ratelimit.blocked(user_key, ratelimit.SIGNIN_ACCOUNT):
        return sign_in_page(request, nxt, error=TOO_MANY, username=username, status_code=429)
    account = None
    stored = None
    try:
        account = accounts.get_by_username(username) if username.strip() else None
        stored = accounts.password_hash(account["id"]) if account else None
    except Exception:  # noqa: BLE001
        logger.warning("sign-in lookup failed", exc_info=True)
        return sign_in_page(request, nxt, error="Sign-in is unavailable right now. Try again in a minute.", username=username, status_code=503)
    ok = passwords.verify(stored, password)
    if not ok or not account or account.get("disabled"):
        ratelimit.hit(client_key, ratelimit.SIGNIN_CLIENT.window)
        ratelimit.hit(user_key, ratelimit.SIGNIN_ACCOUNT.window)
        return sign_in_page(request, nxt, error=GENERIC_SIGNIN_ERROR, username=username, status_code=401)
    ratelimit.clear(user_key)
    if stored and passwords.needs_rehash(stored):
        try:
            accounts.set_password(account["id"], passwords.hash_password(password))
        except Exception:  # noqa: BLE001 - the old hash still works
            logger.warning("password rehash failed", exc_info=True)
    return signin.complete_first_factor(request, account, "password", nxt)


# --- second step -------------------------------------------------------------------------


def _second_step_page(request: Request, *, recovery: bool, error: str = "", status_code: int = 200) -> Response:
    if recovery:
        body = (
            _auth_head("Use a recovery code", "Each of the ten codes you saved works once.")
            + _error(error)
            + '<form class="auth-form" method="post" action="/auth/2fa">'
            + '<input type="hidden" name="kind" value="recovery">'
            + _field("Recovery code", "recovery_code", autocomplete="off",
                     extra='autocapitalize="none" autocorrect="off" spellcheck="false" maxlength="20" autofocus')
            + '<button class="btn btn-primary auth-submit" type="submit">Continue</button></form>'
            + '<p class="auth-foot"><a href="/auth/2fa">Use your authenticator app</a><a href="/auth/login">Start over</a></p>'
        )
    else:
        body = (
            _auth_head("Two-step verification", "Enter the 6-digit code from your authenticator app.")
            + _error(error)
            + '<form class="auth-form" method="post" action="/auth/2fa">'
            + '<input type="hidden" name="kind" value="totp">'
            + _field("Code", "code", autocomplete="one-time-code",
                     extra='inputmode="numeric" pattern="[0-9 ]*" maxlength="9" autofocus', input_cls="acct-code")
            + '<button class="btn btn-primary auth-submit" type="submit">Verify</button></form>'
            + '<p class="auth-foot"><a href="/auth/2fa?recovery=1">Use a recovery code</a><a href="/auth/login">Start over</a></p>'
        )
    replace = ("/auth/2fa?recovery=1" if recovery else "/auth/2fa") if request.method == "POST" else ""
    return _auth_page("Two-step verification", body, status_code=status_code, replace_url=replace)


@router.get("/auth/2fa")
def second_step(request: Request, recovery: int = 0) -> Response:
    off = _sign_in_off(request)
    if off:
        return off
    if not signin.pending_sign_in(request):
        return _redirect("/auth/login")
    return _second_step_page(request, recovery=bool(recovery))


@router.post("/auth/2fa")
async def second_step_submit(request: Request) -> Response:
    off = _sign_in_off(request)
    if off:
        return off
    pending = signin.pending_sign_in(request)
    if not pending:
        return _redirect("/auth/login")
    form = await _form(request)
    recovery = form.get("kind") == "recovery"
    rule = ratelimit.SECOND_STEP_ACCOUNT
    bucket = ratelimit.account_key(rule, pending["aid"])
    if ratelimit.blocked(bucket, rule):
        return _second_step_page(request, recovery=recovery, error=TOO_MANY, status_code=429)
    account = accounts.get_account(pending["aid"])
    if not account or account.get("disabled") or not account.get("totp_enabled_at"):
        response = _redirect("/auth/login")
        response.delete_cookie(signin.PENDING_COOKIE, path="/")
        return response
    accepted = False
    if recovery:
        code = form.get("recovery_code", "")
        if twofactor.normalize_recovery_code(code):
            accepted = accounts.use_recovery_code(account["id"], twofactor.hash_recovery_code(code, signin._secret()))
        used = "recovery"
    else:
        state = accounts.totp_state(account["id"])
        step = twofactor.matching_step(str(state.get("totp_secret") or ""), form.get("code", ""))
        accepted = step is not None and accounts.claim_totp_step(account["id"], step)
        used = "totp"
    if not accepted:
        ratelimit.hit(bucket, rule.window)
        message = "That recovery code is wrong or already used." if recovery else "That code is wrong, expired or already used. Wait for the next one."
        return _second_step_page(request, recovery=recovery, error=message, status_code=401)
    ratelimit.clear(bucket)
    return signin.start_session(request, account, [str(pending.get("m") or "password"), used], str(pending.get("next") or "/"),
                                idp_groups=[str(g) for g in pending.get("g") or []])


# --- confirm it's you --------------------------------------------------------------------


def _confirm_page(request: Request, user: dict[str, Any], nxt: str, *, error: str = "", status_code: int = 200) -> Response:
    has_password = bool(accounts.password_hash(user["account_id"]))
    lede = ("Changes to sign-in, two-step verification and admin settings ask for your password again." if has_password
            else "Changes to sign-in, two-step verification and admin settings ask you to sign in again first.")
    parts = [_auth_head("Confirm it's you", lede), _error(error)]
    if has_password:
        parts.append(
            '<form class="auth-form" method="post" action="/auth/confirm">'
            f'<input type="hidden" name="next" value="{escape(nxt, quote=True)}">'
            f'<input type="hidden" name="username" value="{escape(user.get("username") or "", quote=True)}" autocomplete="username">'
            + _field("Password", "password", type_="password", autocomplete="current-password", extra="autofocus")
            + '<button class="btn btn-primary auth-submit" type="submit">Confirm</button></form>'
        )
    identities = accounts.list_identities(user["account_id"])
    others = []
    for item in signin.available_methods():
        entry = item.reauth_entry(identities, nxt) if item.kind != "password" else None
        if entry:
            others.append(_entry_button(entry, nxt, confirm=True))
    if others:
        if has_password:
            parts.append('<div class="auth-divider" role="separator"><span>or</span></div>')
        parts.append(f'<div class="auth-methods">{"".join(others)}</div>')
    passkey = any("data-passkey-confirm" in item for item in others)
    if passkey:
        parts.append('<p class="auth-note" data-passkey-note role="status" hidden></p>')
    parts.append(f'<p class="auth-foot"><a href="{escape(nxt, quote=True)}">Cancel</a></p>')
    body = "".join(parts)
    if passkey:
        from . import passkeys

        body = f'<div class="auth-passkeys"{passkeys.page_attrs()}>{body}</div>{passkeys.script_tag()}'
    replace = f"/auth/confirm?next={_q(nxt)}" if request.method == "POST" else ""
    return _auth_page("Confirm it's you", body, status_code=status_code, replace_url=replace)


@router.get("/auth/confirm")
def confirm(request: Request, next: str = "/account") -> Response:
    user, redirect = _signed_in_or_redirect(request)
    if redirect:
        return redirect
    return _confirm_page(request, user, signin.safe_next(next, "/account"))  # type: ignore[arg-type]


@router.post("/auth/confirm")
async def confirm_submit(request: Request) -> Response:
    user, redirect = _signed_in_or_redirect(request)
    if redirect:
        return redirect
    assert user is not None
    form = await _form(request)
    nxt = signin.safe_next(form.get("next"), "/account")
    rule = ratelimit.REAUTH_ACCOUNT
    bucket = ratelimit.account_key(rule, user["account_id"])
    if ratelimit.blocked(bucket, rule):
        return _confirm_page(request, user, nxt, error=TOO_MANY, status_code=429)
    if not passwords.verify(accounts.password_hash(user["account_id"]), form.get("password", "")):
        ratelimit.hit(bucket, rule.window)
        return _confirm_page(request, user, nxt, error="That password is wrong.", status_code=401)
    ratelimit.clear(bucket)
    response = _redirect(nxt)
    signin.rotate(request, user, response, reauth=True)
    return response


# --- first run ---------------------------------------------------------------------------


def _setup_available() -> bool:
    settings = get_settings()
    return settings.auth_mode == "accounts" and settings.password_signin_enabled


def _setup_page(error: str = "", values: dict[str, str] | None = None, status_code: int = 200, *, posted: bool = False) -> Response:
    values = values or {}
    body = (
        _auth_head("Set up OpenFeed", "Create the first admin account. Admins add the other people who use this server.")
        + _error(error)
        + '<form class="auth-form" method="post" action="/setup" data-setup-form>'
        + _field("Setup code", "setup_code", value=values.get("setup_code", ""), autocomplete="off",
                 extra='autocapitalize="none" spellcheck="false" maxlength="40"',
                 hint="Printed in the server log at startup. With Docker: docker compose logs openfeed-app | grep \"setup code\"")
        + _username_field(values.get("username", ""), autofocus=False)
        + _field("Display name", "display_name", value=values.get("display_name", ""), autocomplete="name", required=False,
                 extra=f'maxlength="{accounts.DISPLAY_NAME_MAX}"', hint="Optional. Shown in the app instead of the username.")
        + _new_password_fields()
        + '<button class="btn btn-primary auth-submit" type="submit">Create admin account</button></form>'
        + '<p class="auth-foot"><span>No email address is asked for or stored. Keep the recovery codes you can make later, and the '
          "<code>python -m openfeed.tools.accounts</code> command, as your ways back in.</span></p>"
    )
    return _auth_page("Set up OpenFeed", body, status_code=status_code, replace_url="/setup" if posted else "")


@router.get("/setup")
def setup(request: Request) -> Response:
    if not _setup_available():
        return _redirect("/")
    if accounts.admin_exists():
        user = _session_user(request)
        if user and _is_admin(user) and _local_data_pending():
            return _redirect("/setup/data")
        return _redirect("/auth/login")
    signin.setup_code()  # make sure there is one to type
    return _setup_page()


def _check_new_password(form: dict[str, str], username: str = "", display_name: str = "") -> str:
    if form.get("password", "") != form.get("password2", ""):
        raise passwords.PasswordPolicyError("password_mismatch", "The two passwords don't match.")
    return passwords.check_policy(form.get("password", ""), username=username, display_name=display_name)


@router.post("/setup")
async def setup_submit(request: Request) -> Response:
    if not _setup_available():
        return _redirect("/")
    form = await _form(request)
    rule = ratelimit.SETUP_CLIENT
    bucket = ratelimit.client_key(rule, request)
    values = {k: form.get(k, "") for k in ("setup_code", "username", "display_name")}
    if ratelimit.blocked(bucket, rule):
        return _setup_page(TOO_MANY, values, 429, posted=True)
    if accounts.admin_exists():
        return _redirect("/auth/login")
    if not signin.setup_code_matches(form.get("setup_code", "")):
        ratelimit.hit(bucket, rule.window)
        return _setup_page("That setup code is wrong. Copy it from the server log.", values, 401, posted=True)
    try:
        username = accounts.clean_username(form.get("username"))
        display = accounts.clean_display_name(form.get("display_name"), username)
        password = _check_new_password(form, username, display)
        account = accounts.create_account(username, display, role="admin", password_hash=passwords.hash_password(password), first_admin=True)
    except (accounts.AccountError, passwords.PasswordPolicyError) as exc:
        return _setup_page(exc.message, values, 400, posted=True)
    signin.forget_setup_code()
    logger.info("first admin account created")
    return signin.start_session(request, account, ["password"], "/setup/data" if _local_data_pending() else "/")


def _local_data_pending() -> bool:
    try:
        if accounts.get_setting("local_data_adopted"):
            return False
        return any(accounts.local_data_counts().values())
    except Exception:  # noqa: BLE001
        return False


def _counts_sentence(counts: dict[str, int]) -> str:
    labels = (("playback_progress", "watched or started videos"), ("user_video_feedback", "likes and ratings"),
              ("playlists", "playlists"), ("downloads", "downloads"), ("profiles", "household profiles"))
    parts = [f"{counts.get(key, 0):,} {label}" for key, label in labels if counts.get(key)]
    return ", ".join(parts) if parts else "history and preferences"


@router.get("/setup/data")
def setup_data(request: Request) -> Response:
    user, redirect = _signed_in_or_redirect(request)
    if redirect:
        return redirect
    if not _is_admin(user) or not _local_data_pending():
        return _redirect("/")
    counts = accounts.local_data_counts()
    body = (
        _auth_head("Keep the data from before sign-in?",
                   f"Before sign-in was turned on, this server kept {_counts_sentence(counts)} for one shared local user.")
        + '<p class="auth-lede">Keeping it moves all of it, with any household profiles, into your account. '
          "Nobody else can see it there. Starting fresh leaves it untouched and unused; you can still move it later "
          "from Admin, Users.</p>"
        + '<div class="auth-actions">'
        + _post("/setup/data", '<input type="hidden" name="choice" value="keep"><button class="btn btn-primary" type="submit">Keep it in my account</button>')
        + _post("/setup/data", '<input type="hidden" name="choice" value="fresh"><button class="btn" type="submit">Start fresh</button>')
        + "</div>"
    )
    return _auth_page("Keep your data", body)


@router.post("/setup/data")
async def setup_data_submit(request: Request) -> Response:
    user, redirect = _signed_in_or_redirect(request)
    if redirect:
        return redirect
    assert user is not None
    if not _is_admin(user):
        return _redirect("/")
    form = await _form(request)
    if form.get("choice") == "keep":
        counts = accounts.adopt_local_data(user["user_key"])
        logger.info("moved the local user's data to an admin account: %s", {k: v for k, v in counts.items() if v})
        _m()._forget_account_profiles(request)
        response = _redirect("/?done=kept")
        response.delete_cookie("openfeed_profile", path="/")
        return response
    accounts.set_setting("local_data_adopted", {"declined": True})
    return _redirect("/?done=fresh")


# --- sign-up and invites -----------------------------------------------------------------


def _signup_page(title: str, lede: str, action: str, *, error: str = "", values: dict[str, str] | None = None, status_code: int = 200) -> Response:
    """Sign-up or an invite. Drawn with values only as the answer to its POST, when the
    address bar goes back to `action` (its GET address; an invite's link is already there)."""
    posted = values is not None
    values = values or {}
    body = (
        _auth_head(title, lede)
        + _error(error)
        + f'<form class="auth-form" method="post" action="{escape(action, quote=True)}">'
        + _username_field(values.get("username", ""))
        + _field("Display name", "display_name", value=values.get("display_name", ""), autocomplete="name", required=False,
                 extra=f'maxlength="{accounts.DISPLAY_NAME_MAX}"', hint="Optional.")
        + _new_password_fields()
        # A field people never see. Forms filled in by bots tend to fill it.
        + '<div class="acct-trap" aria-hidden="true"><label>Leave this empty<input name="website" tabindex="-1" autocomplete="off"></label></div>'
        + '<button class="btn btn-primary auth-submit" type="submit">Create account</button></form>'
        + '<p class="auth-foot"><span>No email address is needed. If you forget your password, an admin can give you a reset link.</span>'
          '<a href="/auth/login">I already have an account</a></p>'
    )
    return _auth_page(title, body, status_code=status_code, replace_url=action if posted else "")


@router.get("/auth/signup")
def signup(request: Request) -> Response:
    off = _sign_in_off(request)
    if off:
        return off
    if not get_settings().password_signin_enabled or accounts.registration_mode() != "open":
        return _redirect("/auth/login")
    return _signup_page("Create an account", "Your history, likes, playlists and recommendations stay in your own account.", "/auth/signup")


@router.post("/auth/signup")
async def signup_submit(request: Request) -> Response:
    off = _sign_in_off(request)
    if off:
        return off
    if not get_settings().password_signin_enabled or accounts.registration_mode() != "open":
        return _redirect("/auth/login")
    form = await _form(request)
    values = {k: form.get(k, "") for k in ("username", "display_name")}
    lede = "Your history, likes, playlists and recommendations stay in your own account."
    per_client = ratelimit.client_key(ratelimit.SIGNUP_CLIENT, request)
    everyone = ratelimit.global_key(ratelimit.SIGNUP_GLOBAL)
    if ratelimit.blocked(per_client, ratelimit.SIGNUP_CLIENT) or ratelimit.blocked(everyone, ratelimit.SIGNUP_GLOBAL):
        return _signup_page("Create an account", lede, "/auth/signup", error="Too many new accounts right now. Try again in an hour.", values=values, status_code=429)
    if form.get("website"):
        ratelimit.hit(per_client, ratelimit.SIGNUP_CLIENT.window)
        return _redirect("/auth/login")
    try:
        username = accounts.clean_username(form.get("username"))
        display = accounts.clean_display_name(form.get("display_name"), username)
        password = _check_new_password(form, username, display)
        ratelimit.hit(per_client, ratelimit.SIGNUP_CLIENT.window)
        ratelimit.hit(everyone, ratelimit.SIGNUP_GLOBAL.window)
        account = accounts.create_account(username, display, password_hash=passwords.hash_password(password))
    except (accounts.AccountError, passwords.PasswordPolicyError) as exc:
        return _signup_page("Create an account", lede, "/auth/signup", error=exc.message, values=values, status_code=400)
    return signin.start_session(request, account, ["password"], "/")


def _invite_link(token: str) -> str:
    return f"{get_settings().public_url}/auth/invite/{token}"


def _invite_providers(token: str) -> list[signin.SignInMethod]:
    """OIDC providers an invited person may create their account with."""
    return [m for m in signin.available_methods() if m.kind == "oidc" and getattr(m, "name", "")]


def _invite_buttons(token: str) -> str:
    rows = [
        f'<a class="btn auth-method" href="/auth/oidc/{escape(getattr(m, "name", ""), quote=True)}/start?invite={_q(token)}" '
        f'data-signin-method="{escape(m.key, quote=True)}">{icon("external")}<span>Create it with {escape(getattr(m, "display", m.label))}</span></a>'
        for m in _invite_providers(token)
    ]
    return f'<div class="auth-methods" data-invite-methods>{"".join(rows)}</div>' if rows else ""


@router.get("/auth/invite/{token}")
def invite(request: Request, token: str) -> Response:
    off = _sign_in_off(request)
    if off:
        return off
    password_on = get_settings().password_signin_enabled
    usable = password_on or bool(_invite_providers(token))
    link = accounts.find_link("invite", signin.token_hash(token)) if usable else None
    if not link or accounts.registration_mode() == "closed":
        return _auth_page("Invite link", _auth_head("This invite link doesn't work", "It has expired, was already used, or invites are turned off. Ask for a new one.")
                          + '<p class="auth-foot"><a href="/auth/login">Sign in</a></p>', status_code=410)
    lede = "Create your OpenFeed account. Your history and recommendations are yours alone."
    buttons = _invite_buttons(token)
    if not password_on:
        return _auth_page("You're invited", _auth_head("You're invited", lede) + buttons
                          + '<p class="auth-foot"><a href="/auth/login">I already have an account</a></p>')
    page = _signup_page("You're invited", lede, f"/auth/invite/{token}")
    if buttons and isinstance(page, HTMLResponse):
        html = page.body.decode("utf-8").replace('<p class="auth-foot">', '<div class="auth-divider" role="separator"><span>or</span></div>' + buttons + '<p class="auth-foot">', 1)
        page = _no_store(HTMLResponse(html, status_code=page.status_code))
    return page


@router.post("/auth/invite/{token}")
async def invite_submit(request: Request, token: str) -> Response:
    off = _sign_in_off(request)
    if off:
        return off
    if not get_settings().password_signin_enabled or accounts.registration_mode() == "closed":
        return _redirect("/auth/login")
    form = await _form(request)
    values = {k: form.get(k, "") for k in ("username", "display_name")}
    lede = "Create your OpenFeed account. Your history and recommendations are yours alone."
    rule = ratelimit.SIGNUP_CLIENT
    bucket = ratelimit.client_key(rule, request)
    if ratelimit.blocked(bucket, rule):
        return _signup_page("You're invited", lede, f"/auth/invite/{token}", error="Too many attempts. Try again in an hour.", values=values, status_code=429)
    try:
        username = accounts.clean_username(form.get("username"))
        display = accounts.clean_display_name(form.get("display_name"), username)
        password = _check_new_password(form, username, display)
        account = accounts.redeem_invite(signin.token_hash(token), username, display, passwords.hash_password(password))
    except (accounts.AccountError, passwords.PasswordPolicyError) as exc:
        if getattr(exc, "code", "") == "invite_invalid":
            ratelimit.hit(bucket, rule.window)
        return _signup_page("You're invited", lede, f"/auth/invite/{token}", error=exc.message, values=values, status_code=400)
    return signin.start_session(request, account, ["password"], "/")


@router.get("/auth/reset/{token}")
def reset(request: Request, token: str) -> Response:
    off = _sign_in_off(request)
    if off:
        return off
    link = accounts.find_link("reset", signin.token_hash(token))
    if not link:
        return _auth_page("Reset link", _auth_head("This reset link doesn't work", "It has expired or was already used. Ask an admin for a new one.")
                          + '<p class="auth-foot"><a href="/auth/login">Sign in</a></p>', status_code=410)
    return _reset_page(token, str(link.get("username") or ""))


def _reset_page(token: str, username: str, error: str = "", status_code: int = 200) -> Response:
    # Drawn with an error only as the answer to its POST; the link is already in the address bar.
    body = (
        _auth_head("Choose a new password", f"For the account {username}." if username else "")
        + _error(error)
        + f'<form class="auth-form" method="post" action="/auth/reset/{escape(token, quote=True)}">'
        + f'<input type="hidden" name="username" value="{escape(username, quote=True)}" autocomplete="username">'
        + _new_password_fields("new")
        + '<button class="btn btn-primary auth-submit" type="submit">Set password</button></form>'
        + '<p class="auth-foot"><span>Every device signed in to this account will be signed out.</span></p>'
    )
    return _auth_page("Choose a new password", body, status_code=status_code, replace_url=f"/auth/reset/{_q(token)}" if error else "")


@router.post("/auth/reset/{token}")
async def reset_submit(request: Request, token: str) -> Response:
    off = _sign_in_off(request)
    if off:
        return off
    form = await _form(request)
    link = accounts.find_link("reset", signin.token_hash(token))
    if not link:
        return reset(request, token)
    username = str(link.get("username") or "")
    try:
        password = _check_new_password(form, username, str(link.get("display_name") or ""))
        accounts.redeem_reset(signin.token_hash(token), passwords.hash_password(password))
    except (accounts.AccountError, passwords.PasswordPolicyError) as exc:
        return _reset_page(token, username, exc.message, 400)
    response = _redirect("/auth/login?done=password_set")
    signin.clear_session_cookie(response)
    return response


# --- Account & security ------------------------------------------------------------------


def _reauth_banner(user: dict[str, Any], back: str) -> str:
    if signin.reauth_fresh(user):
        return '<p class="acct-unlocked">' + icon("shield") + "<span>Security settings are unlocked for a few minutes.</span></p>"
    return (
        f'<div class="acct-lock">{icon("shield")}<div><strong>Changes below ask for your password first</strong>'
        f"<small>Confirming once keeps them unlocked for {max(1, get_settings().reauth_seconds // 60)} minutes.</small></div>"
        f'<a class="btn btn-sm" href="/auth/confirm?next={_q(back)}">Confirm it&rsquo;s you</a></div>'
    )


def _signed_in_at(value: Any) -> str:
    """When a session signed in, as an absolute time: a <time> the account script shows in
    the viewer's own time zone (UTC until it runs)."""
    from datetime import datetime, timezone

    if not isinstance(value, datetime):
        return ""
    when = (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
    text = f"{when:%b} {when.day}, {when:%H:%M} UTC"
    return f'<time datetime="{when.isoformat()}" data-local-time>{escape(text)}</time>'


def session_tag(session_id: Any) -> str:
    """A short tag for a session, the same on every device's list, so "This device" on one
    can be found in the list on another. Part of the row id; nothing about the device."""
    return str(session_id or "").replace("-", "")[-4:].upper()


def _sessions_group(user: dict[str, Any], rows: list[dict[str, Any]], *, admin_view: bool = False) -> str:
    items = []
    # This device first, then the most recently active.
    rows = sorted(rows, key=lambda row: row["id"] != user.get("session_id"))
    for row in rows:
        current = row["id"] == user.get("session_id")
        badge = '<span class="acct-badge is-on">This device</span>' if current else ""
        methods = ", ".join(signin.labels_for([m for m in row.get("methods") or [] if m not in {"totp", "recovery", "passkey-uv"}]))
        second = " + 2FA" if any(m in {"totp", "recovery"} for m in row.get("methods") or []) else ""
        when = _signed_in_at(row.get("created_at"))
        note = (f"Signed in {escape(_rel(row.get('created_at')))}{f' ({when})' if when else ''} "
                f"with {escape(methods or 'a sign-in method')}{second} · Active {escape(_rel(row.get('last_seen_at')))}")
        tag = session_tag(row.get("id"))
        label = f'{row.get("device") or "Unknown device"}' + (f" · {tag}" if tag else "")
        action = "" if current else _post(
            f"/account/sessions/{row['id']}/revoke",
            f'<button class="btn btn-sm" type="submit" aria-label="Sign out {escape(label, quote=True)}">Sign out</button>',
        )
        tag_html = (' <span class="acct-session-tag" title="Session tag: the same on every device&#x27;s list">'
                    f"{escape(tag)}</span>") if tag else ""
        items.append(
            f'<div class="set-row acct-row{" is-current" if current else ""}" data-session-row><span class="acct-icon">{icon("user")}</span>'
            f'<div class="set-copy"><strong>{escape(row.get("device") or "Unknown device")}{tag_html} {badge}</strong>'
            f'<small>{note}</small></div>'
            f'<div class="set-control">{action}</div></div>'
        )
    others = len(rows) > 1
    foot = _post("/account/sessions/revoke-others", '<button class="btn" type="submit">Sign out everywhere else</button>', cls="acct-foot-form") if others else ""
    return _m()._set_group("Devices", "".join(items) or _m()._set_row("No other devices", "Only this browser is signed in."), foot)


def _methods_group(user: dict[str, Any], account: dict[str, Any], identities: list[dict[str, Any]], extra: str = "") -> str:
    main = _m()
    rows = []
    for identity in identities:
        if identity["method"] == "passkey":
            continue  # listed in their own group (passkeys.account_group)
        item = signin.method(identity["method"])
        info = item.describe(identity) if item else {"label": identity["method"], "kind": ""}
        note = f"Added {_rel(identity.get('created_at'))}"
        if identity.get("last_used_at"):
            note += f" · Last used {_rel(identity.get('last_used_at'))}"
        if not signin.usable(identity):
            note += " · Not available on this server now"
        control = ""
        if signin.can_remove(identities, identity["id"]):
            control = _post(
                f"/account/methods/{identity['id']}/remove",
                f'<button class="btn btn-sm btn-danger" type="submit" aria-label="Remove {escape(info["label"], quote=True)}">Remove</button>',
                confirm={"title": f"Remove {info['label']}?", "body": "You will no longer be able to sign in this way.", "label": "Remove"},
            )
        rows.append(main._set_row(info["label"], note, control))
    links = []
    for item in signin.available_methods():
        if item.can_link(account, identities):
            href = item.link_href("/account")
            if href:
                links.append(f'<a class="btn btn-sm" href="{escape(href, quote=True)}">{icon("plus")}<span>Link {escape(getattr(item, "display", item.label))}</span></a>')
    links_html = "".join(links) + extra
    foot = f'<div class="acct-foot-form">{links_html}</div>' if links_html else ""
    return main._set_group("Sign-in methods", "".join(rows) or main._set_row("Passkeys only", "This account signs in with the passkeys below."), foot)


def _two_step_group(user: dict[str, Any], account: dict[str, Any]) -> str:
    main = _m()
    if account.get("totp_enabled_at"):
        left = accounts.recovery_codes_left(account["id"])
        rows = main._set_row(
            "Authenticator app", f"On since {_rel(account.get('totp_enabled_at'))}. Asked for after your password.",
            _post("/account/2fa/off", '<button class="btn btn-sm btn-danger" type="submit">Turn off</button>',
                  confirm={"title": "Turn off two-step verification?", "body": "Signing in will need only your password.", "label": "Turn off"}),
        ) + main._set_row(
            "Recovery codes", f"{left} of {twofactor.RECOVERY_CODE_COUNT} unused. Each one gets you in once if you lose your phone.",
            _post("/account/recovery-codes", '<button class="btn btn-sm" type="submit">Make new codes</button>',
                  confirm={"title": "Make new recovery codes?", "body": "The codes you saved before stop working.", "label": "Make new codes"}),
        )
    else:
        rows = main._set_row(
            "Authenticator app", "Off. Turn it on to ask for a 6-digit code from an app on your phone after your password.",
            '<a class="btn btn-sm" href="/account/2fa">Set up</a>',
        )
    return main._set_group("Two-step verification", rows)


def _password_group(account: dict[str, Any], has_password: bool, *, current_error: str = "", new_error: str = "") -> str:
    """The Password group. current_error and new_error: a refused change, shown under the
    field it is about."""
    main = _m()
    if not get_settings().password_signin_enabled:
        return ""
    if has_password:
        form = (
            '<form class="acct-form" method="post" action="/account/password" id="password">'
            f'<input type="hidden" name="username" value="{escape(account["username"], quote=True)}" autocomplete="username">'
            + _field("Current password", "current_password", type_="password", autocomplete="current-password", error=current_error)
            + _new_password_fields("new", error=new_error)
            + '<div class="acct-form-actions"><button class="btn" type="submit">Change password</button></div></form>'
        )
        return main._set_group("Password", form, '<p class="set-foot">Changing it signs out your other devices.</p>')
    form = (
        '<form class="acct-form" method="post" action="/account/password" id="password">'
        f'<input type="hidden" name="username" value="{escape(account["username"], quote=True)}" autocomplete="username">'
        + _new_password_fields("new", error=new_error)
        + '<div class="acct-form-actions"><button class="btn" type="submit">Set a password</button></div></form>'
    )
    ways = ", ".join(dict.fromkeys(signin.labels_for([i["method"] for i in accounts.list_identities(account["id"])]))) or "another method"
    return main._set_group("Password", form, f'<p class="set-foot">You sign in with {escape(ways)} today. A password adds another way in, with the username {escape(account["username"])}.</p>')


@router.get("/account")
def account_page(request: Request) -> Response:
    user, redirect = _signed_in_or_redirect(request)
    if redirect:
        return redirect
    assert user is not None
    return _account_page(request, user)


def _account_page(request: Request, user: dict[str, Any], *, current_error: str = "", new_error: str = "",
                  status_code: int = 200) -> Response:
    """Account & security. The errors are a refused password change, drawn as the answer to
    its POST: next to the field, with the address bar back on /account."""
    main = _m()
    account = _account_of(user)
    identities = accounts.list_identities(account["id"])
    has_password = any(i["method"] == "password" for i in identities)
    role = "Admin" if account["role"] == "admin" else "Member"
    profile_rows = (
        main._set_row("Username", "How you sign in. It can't be changed.", f'<code class="acct-code-text">{escape(account["username"])}</code>')
        + '<div class="set-row"><div class="set-copy"><strong>Display name</strong><small>Shown in the app and on the main profile.</small></div>'
        + '<div class="set-control">'
        + _post("/account/name", f'<input class="field-input acct-inline-input" name="display_name" maxlength="{accounts.DISPLAY_NAME_MAX}" '
                f'value="{escape(account.get("display_name") or "", quote=True)}" aria-label="Display name" autocomplete="name">'
                '<button class="btn btn-sm" type="submit">Save</button>', cls="acct-inline")
        + "</div></div>"
        + main._set_row("Role", "Admins manage users and install-wide settings." if role == "Admin" else "Ask an admin if you need more access.",
                        f'<span class="acct-badge{" is-on" if role == "Admin" else ""}">{role}</span>')
    )
    admin_link = (
        main._set_group("Administration", main._set_row("Users", "Accounts, invites, registration and password resets.",
                                                       f'<a class="btn btn-sm" href="/admin/users">{icon("users")}<span>Manage users</span></a>'))
        if _is_admin(user) else ""
    )
    show_2fa = get_settings().password_signin_enabled or account.get("totp_enabled_at")
    danger = main._set_group(
        "Delete account",
        '<form class="acct-form" method="post" action="/account/delete">'
        '<p class="acct-form-note">Deletes your account, every profile under it and all of their history, likes, playlists, '
        "preferences, API tokens and downloads. This can't be undone.</p>"
        + _field("Type your username to confirm", "confirm", autocomplete="off", extra='autocapitalize="none" spellcheck="false"')
        + '<div class="acct-form-actions"><button class="btn btn-danger" type="submit">Delete my account</button></div></form>',
    )
    from . import passkeys, trusted_header

    content = (
        main._page_head("Account & security", f"Signed in as {account['username']}")
        + _notice(request)
        + _reauth_banner(user, "/account")
        + main._set_group("Account", profile_rows)
        + _password_group(account, has_password, current_error=current_error, new_error=new_error)
        + (_two_step_group(user, account) if show_2fa else "")
        + passkeys.account_group(user, identities, rel=_rel)
        + _methods_group(user, account, identities, trusted_header.link_offer(request, identities))
        + _sessions_group(user, accounts.list_sessions(account["id"]))
        + admin_link
        + danger
    )
    posted = request.method == "POST"
    response = _app_page(request, "Account & security", content, replace_url="/account" if posted else "")
    response.status_code = status_code
    return response


@router.post("/account/name")
async def account_name(request: Request) -> Response:
    user, redirect = _signed_in_or_redirect(request)
    if redirect:
        return redirect
    assert user is not None
    form = await _form(request)
    try:
        name = accounts.clean_display_name(form.get("display_name"), user.get("username") or "")
    except accounts.AccountError:
        return _redirect("/account?error=not_found")
    accounts.update_account(user["account_id"], display_name=name)
    return _redirect("/account?done=name")


@router.post("/account/password")
async def account_password(request: Request) -> Response:
    user, redirect = _signed_in_or_redirect(request)
    if redirect:
        return redirect
    assert user is not None
    form = await _form(request)
    account = _account_of(user)
    stored = accounts.password_hash(account["id"])
    rule = ratelimit.REAUTH_ACCOUNT
    bucket = ratelimit.account_key(rule, account["id"])
    if stored:
        if ratelimit.blocked(bucket, rule):
            return _account_page(request, user, current_error="Too many wrong passwords. Wait 15 minutes and try again.", status_code=429)
        if not passwords.verify(stored, form.get("current_password", "")):
            ratelimit.hit(bucket, rule.window)
            return _account_page(request, user, current_error="That isn't your current password. Nothing was changed.", status_code=401)
    elif not signin.reauth_fresh(user):
        return _reauth_redirect("/account")
    try:
        password = _check_new_password(form, account["username"], account.get("display_name") or "")
    except passwords.PasswordPolicyError as exc:
        return _account_page(request, user, new_error=f"{exc.message} Nothing was changed.", status_code=400)
    accounts.set_password(account["id"], passwords.hash_password(password))
    accounts.delete_sessions(account["id"], except_session_id=user["session_id"])
    response = _redirect("/account?done=" + ("password" if stored else "password_set"))
    signin.rotate(request, user, response, reauth=True)
    return response


@router.get("/account/2fa")
def two_step_setup(request: Request) -> Response:
    user, redirect = _signed_in_or_redirect(request)
    if redirect:
        return redirect
    assert user is not None
    if not signin.reauth_fresh(user):
        return _reauth_redirect("/account/2fa")
    account = _account_of(user)
    if account.get("totp_enabled_at"):
        return _redirect("/account")
    # A reload (after a wrong code, say) keeps the key they may already have scanned.
    secret = str(accounts.totp_state(account["id"]).get("totp_pending_secret") or "")
    if not secret:
        secret = twofactor.new_secret()
        accounts.set_totp_pending(account["id"], secret)
    return _two_step_setup_page(request, account, secret)


def _two_step_setup_page(request: Request, account: dict[str, Any], secret: str, error: str = "", status_code: int = 200) -> Response:
    main = _m()
    uri = twofactor.provisioning_uri(secret, account["username"], issuer=_issuer())
    content = (
        main._page_head("Set up two-step verification")
        + _error(error)
        + '<div class="acct-totp">'
        + f'<figure class="acct-qr">{twofactor.qr_svg(uri)}<figcaption>Scan with an authenticator app such as Aegis, 2FAS, '
          "Google Authenticator or 1Password. The code is drawn on this server; nothing is sent anywhere.</figcaption></figure>"
        + '<div class="acct-totp-steps">'
        + '<ol class="acct-steps"><li>Open your authenticator app and add an account.</li>'
          "<li>Scan the code, or type the key below.</li><li>Enter the 6-digit code the app shows.</li></ol>"
        + f'<div class="acct-secret"><span class="set-label">Setup key</span><code data-copy-text>{escape(twofactor.format_secret(secret))}</code>'
          f'<button class="btn btn-sm" type="button" data-copy-target hidden>Copy</button></div>'
        + '<form class="acct-form" method="post" action="/account/2fa">'
        + _field("Code from the app", "code", autocomplete="one-time-code", extra='inputmode="numeric" pattern="[0-9 ]*" maxlength="9" autofocus', input_cls="acct-code")
        + '<div class="acct-form-actions"><button class="btn btn-primary" type="submit">Turn on</button>'
          '<a class="btn" href="/account">Cancel</a></div></form>'
        + "</div></div>"
    )
    response = _app_page(request, "Two-step verification", content, replace_url="/account/2fa" if request.method == "POST" else "")
    response.status_code = status_code
    return response


def _issuer() -> str:
    host = urllib.parse.urlsplit(get_settings().public_url).hostname or ""
    return f"OpenFeed ({host})" if host and host not in {"localhost", "127.0.0.1"} else "OpenFeed"


@router.post("/account/2fa")
async def two_step_setup_submit(request: Request) -> Response:
    user, redirect = _signed_in_or_redirect(request)
    if redirect:
        return redirect
    assert user is not None
    if not signin.reauth_fresh(user):
        return _reauth_redirect("/account/2fa")
    form = await _form(request)
    account = _account_of(user)
    state = accounts.totp_state(account["id"])
    secret = str(state.get("totp_pending_secret") or "")
    if not secret:
        return _redirect("/account/2fa")
    step = twofactor.matching_step(secret, form.get("code", ""))
    if step is None:
        return _two_step_setup_page(request, account, secret, "That code doesn't match. Check the time on your phone and try the next code.", 400)
    codes = twofactor.new_recovery_codes()
    if not accounts.enable_totp(account["id"], secret, step, [twofactor.hash_recovery_code(c, signin._secret()) for c in codes]):
        return _redirect("/account/2fa")
    response = _recovery_codes_page(request, codes, first=True)
    signin.rotate(request, user, response, reauth=True)
    return response


def _recovery_codes_page(request: Request, codes: list[str], *, first: bool) -> Response:
    """The new recovery codes, shown this once, as the answer to the POST that made them.
    The address bar moves to /account?done=..., so Reload or Back shows the account page
    with a notice and never the codes (nor makes new ones)."""
    main = _m()
    rows = (len(codes) + 1) // 2
    items = "".join(
        f'<li><span class="acct-code-num" aria-hidden="true">{n}.</span><code>{escape(code)}</code></li>'
        for n, code in enumerate(codes, start=1)
    )
    text = "\n".join(codes)
    content = (
        main._page_head("Recovery codes")
        + (f'<p class="acct-notice" role="status">{icon("check")}<span>Two-step verification is on.</span></p>' if first else "")
        + '<p class="settings-lede">If you lose your phone, each of these codes signs you in once in place of an app code. '
          "Save them somewhere safe now: they are shown only this once.</p>"
        + f'<ol class="acct-codes" role="list" data-recovery-codes style="--code-rows: {rows}">{items}</ol>'
        + '<div class="acct-form-actions">'
        + f'<button class="btn" type="button" data-copy-codes="{escape(text, quote=True)}" hidden>Copy all</button>'
        + f'<a class="btn" download="openfeed-recovery-codes.txt" href="data:text/plain;charset=utf-8,{urllib.parse.quote(text)}">Download as text</a>'
        + '<a class="btn btn-primary" href="/account">I saved them</a></div>'
    )
    return _app_page(request, "Recovery codes", content, replace_url="/account?done=" + ("2fa_on" if first else "codes"))


@router.post("/account/2fa/off")
async def two_step_off(request: Request) -> Response:
    user, redirect = _signed_in_or_redirect(request)
    if redirect:
        return redirect
    assert user is not None
    if not signin.reauth_fresh(user):
        return _reauth_redirect("/account")
    accounts.disable_totp(user["account_id"])
    response = _redirect("/account?done=2fa_off")
    signin.rotate(request, user, response)
    return response


@router.post("/account/recovery-codes")
async def recovery_codes(request: Request) -> Response:
    user, redirect = _signed_in_or_redirect(request)
    if redirect:
        return redirect
    assert user is not None
    if not signin.reauth_fresh(user):
        return _reauth_redirect("/account")
    account = _account_of(user)
    if not account.get("totp_enabled_at"):
        return _redirect("/account")
    codes = twofactor.new_recovery_codes()
    accounts.replace_recovery_codes(account["id"], [twofactor.hash_recovery_code(c, signin._secret()) for c in codes])
    return _recovery_codes_page(request, codes, first=False)


@router.post("/account/methods/{identity_id}/remove")
async def remove_method(request: Request, identity_id: str) -> Response:
    user, redirect = _signed_in_or_redirect(request)
    if redirect:
        return redirect
    assert user is not None
    if not signin.reauth_fresh(user):
        return _reauth_redirect("/account")
    account = _account_of(user)
    identities = accounts.list_identities(account["id"])
    identity = next((i for i in identities if i["id"] == identity_id), None)
    if not identity:
        return _redirect("/account?error=not_found")
    # Something usable here must remain (a provider no longer configured doesn't count).
    if not signin.can_remove(identities, identity_id):
        return _redirect("/account?error=last_method")
    item = signin.method(identity["method"])
    try:
        removed = item.remove(account, identity) if item else accounts.remove_identity(account["id"], identity_id)
    except accounts.AccountError as exc:
        return _redirect(f"/account?error={exc.code}")
    return _redirect("/account?done=method_removed" if removed else "/account?error=not_found")


@router.post("/account/sessions/{session_id}/revoke")
async def revoke_session(request: Request, session_id: str) -> Response:
    user, redirect = _signed_in_or_redirect(request)
    if redirect:
        return redirect
    assert user is not None
    try:
        removed = accounts.delete_session(user["account_id"], session_id)
    except Exception:  # noqa: BLE001 - a malformed id
        removed = False
    return _redirect("/account?done=session" if removed else "/account?error=not_found")


@router.post("/account/sessions/revoke-others")
async def revoke_other_sessions(request: Request) -> Response:
    user, redirect = _signed_in_or_redirect(request)
    if redirect:
        return redirect
    assert user is not None
    accounts.delete_sessions(user["account_id"], except_session_id=user["session_id"])
    return _redirect("/account?done=others")


@router.post("/account/delete")
async def delete_my_account(request: Request) -> Response:
    user, redirect = _signed_in_or_redirect(request)
    if redirect:
        return redirect
    assert user is not None
    if not signin.reauth_fresh(user):
        return _reauth_redirect("/account")
    form = await _form(request)
    if form.get("confirm", "").strip().lower() != str(user.get("username") or "").lower():
        return _redirect("/account?error=confirm_name")
    try:
        result = accounts.delete_account(user["account_id"])
    except accounts.AccountError as exc:
        return _redirect(f"/account?error={exc.code}")
    if result:
        _m()._after_user_keys_deleted(result["user_keys"], result["downloads"])
    response = _redirect("/auth/login")
    signin.clear_session_cookie(response)
    response.delete_cookie("openfeed_profile", path="/")
    return response


# --- admin: users ------------------------------------------------------------------------


def _require_admin(request: Request) -> tuple[dict[str, Any] | None, Response | None]:
    user, redirect = _signed_in_or_redirect(request)
    if redirect:
        return None, redirect
    if not _is_admin(user):
        return None, _m()._access_denied_response(user, admin_only=True)
    return user, None


def _admin_action_guard(request: Request) -> tuple[dict[str, Any] | None, Response | None]:
    user, refusal = _require_admin(request)
    if refusal:
        return None, refusal
    if not signin.reauth_fresh(user):
        return None, _reauth_redirect("/admin/users")
    return user, None


def _account_row(admin: dict[str, Any], row: dict[str, Any]) -> str:
    main = _m()
    rid = escape(row["id"], quote=True)
    me = row["id"] == admin.get("account_id")
    name = row.get("display_name") or row["username"]
    badges = []
    if row["role"] == "admin":
        badges.append('<span class="acct-badge is-on">Admin</span>')
    if row.get("disabled"):
        badges.append('<span class="acct-badge is-warn">Disabled</span>')
    if me:
        badges.append('<span class="acct-badge">You</span>')
    meta = [f"@{row['username']}", "2FA on" if row.get("totp_enabled_at") else "2FA off",
            ", ".join(signin.labels_for(list(row.get("methods") or []))) or "No sign-in method",
            f"Created {_rel(row.get('created_at'))}", f"Active {_rel(row.get('last_active_at'))}"]
    actions = ""
    if not me:
        role_target = "member" if row["role"] == "admin" else "admin"
        role_label = "Make member" if row["role"] == "admin" else "Make admin"
        toggle = ("enable", "Enable") if row.get("disabled") else ("disable", "Disable")
        buttons = [
            _post(f"/admin/users/{rid}/role", f'<input type="hidden" name="role" value="{role_target}"><button class="btn btn-sm" type="submit">{role_label}</button>',
                  confirm={"title": f"{role_label}: {name}?", "body": "Their sessions end, so the change applies when they sign in again.", "label": role_label}),
            _post(f"/admin/users/{rid}/{toggle[0]}", f'<button class="btn btn-sm" type="submit">{toggle[1]}</button>',
                  confirm={"title": f"{toggle[1]} {name}?", "body": "A disabled account is signed out everywhere and its API tokens stop working." if toggle[0] == "disable" else "They can sign in again.", "label": toggle[1]}),
            _post(f"/admin/users/{rid}/reset-link", '<button class="btn btn-sm" type="submit">Password reset link</button>'),
        ]
        if row.get("totp_enabled_at"):
            buttons.append(_post(f"/admin/users/{rid}/reset-2fa", '<button class="btn btn-sm" type="submit">Turn off 2FA</button>',
                                 confirm={"title": f"Turn off two-step verification for {name}?", "body": "Use this when they lost their phone and their recovery codes.", "label": "Turn off"}))
        if row.get("sessions"):
            buttons.append(_post(f"/admin/users/{rid}/sign-out", '<button class="btn btn-sm" type="submit">Sign out everywhere</button>'))
        delete = (
            f'<details class="acct-delete"><summary class="btn btn-sm btn-danger">Delete…</summary>'
            + _post(f"/admin/users/{rid}/delete",
                    '<p class="acct-form-note">Deletes the account, its profiles and all of their history, likes, playlists, tokens and downloads.</p>'
                    + _field("Type the username to confirm", "confirm", autocomplete="off", field_id=f"del-{rid}", extra='autocapitalize="none" spellcheck="false"')
                    + '<div class="acct-form-actions"><button class="btn btn-danger btn-sm" type="submit">Delete account</button></div>', cls="acct-form")
            + "</details>"
        )
        actions = (
            f'<details class="acct-manage"><summary class="btn btn-sm" aria-label="Manage {escape(name, quote=True)}">Manage</summary>'
            f'<div class="acct-manage-panel">{"".join(buttons)}{delete}</div></details>'
        )
    else:
        actions = '<a class="btn btn-sm" href="/account">Your account</a>'
    return (
        f'<div class="set-row acct-row acct-user" data-account-row="{rid}">'
        f'<span class="profile-dot">{escape(main._avatar_initials(name))}</span>'
        f'<div class="set-copy"><strong>{escape(name)} {"".join(badges)}</strong><small>{escape(" · ".join(meta))}</small></div>'
        f'<div class="set-control">{actions}</div></div>'
    )


@router.get("/admin/users")
def admin_users(request: Request) -> Response:
    user, refusal = _require_admin(request)
    if refusal:
        return refusal
    assert user is not None
    return _admin_users_page(request, user)


def _admin_users_page(request: Request, user: dict[str, Any], *, reveal: str = "", status_code: int = 200,
                      replace_url: str = "") -> Response:
    """Admin, Users. reveal: a one-time link or an error, drawn as the answer to a POST;
    replace_url is then the GET address (without the link) the address bar moves to."""
    main = _m()
    rows = accounts.list_accounts()
    mode = accounts.registration_mode()
    password_on = get_settings().password_signin_enabled
    # Registration also decides who an OIDC provider or the trusted proxy may create.
    signups = password_on or bool(_invite_providers("")) or any(m.kind == "header" for m in signin.available_methods())
    registration = main._set_group(
        "Registration",
        _post("/admin/users/registration",
              '<div class="set-row"><div class="set-copy"><strong>Who can create an account</strong>'
              "<small>Closed: only admins add people. Invite links: anyone with a link you made. Open: anyone who can reach this server.</small></div>"
              '<div class="set-control acct-inline">'
              + main._select("mode", [("closed", "Closed"), ("invite", "Invite links"), ("open", "Open sign-up")], mode, "Registration")
              + '<button class="btn btn-sm" type="submit">Save</button></div></div>'),
        '<p class="set-foot">Open sign-up is for public servers. New accounts are rate-limited per client and per hour; there is no '
        "third-party captcha and no email. Each account's history and recommendations are its own.</p>",
    ) if signups else ""
    invites = ""
    if signups and mode != "closed":
        live = accounts.list_links("invite")
        invite_rows = "".join(
            main._set_row(
                f"{'Admin' if link['role'] == 'admin' else 'Member'} invite" + (f" · {link['note']}" if link.get("note") else ""),
                f"Made {_rel(link.get('created_at'))} by {link.get('created_by_name') or 'an admin'} · {_until(link.get('expires_at'))}",
                _post(f"/admin/invites/{escape(link['id'], quote=True)}/revoke", '<button class="btn btn-sm" type="submit">Revoke</button>'),
            )
            for link in live
        )
        form = _post(
            "/admin/invites",
            '<div class="set-row"><div class="set-copy"><strong>New invite link</strong><small>Works once. Send it however you like; OpenFeed never emails.</small></div>'
            '<div class="set-control acct-inline">'
            + main._select("role", [("member", "Member"), ("admin", "Admin")], "member", "Role for the invited account")
            + main._select("days", [(str(d), f"{d} day{'s' if d > 1 else ''}") for d in INVITE_DAYS], "7", "Link lifetime")
            + '<input class="field-input acct-inline-input" name="note" maxlength="60" placeholder="Note (optional)" aria-label="Note">'
            + '<button class="btn btn-sm" type="submit">Make link</button></div></div>',
        )
        invites = main._set_group("Invite links", form + invite_rows)
    create = main._set_group(
        "Add an account",
        '<form class="acct-form acct-create" method="post" action="/admin/users/create">'
        + _field("Username", "username", autocomplete="off", extra='autocapitalize="none" spellcheck="false" maxlength="64"', field_id="new-username")
        + _field("Display name", "display_name", required=False, autocomplete="off", extra=f'maxlength="{accounts.DISPLAY_NAME_MAX}"', field_id="new-display")
        + f'<label class="text-field acct-field"><span class="set-label">Role</span>{main._select("role", [("member", "Member"), ("admin", "Admin")], "member", "Role")}</label>'
        + '<p class="acct-form-note">You get a one-time link to send them, where they choose their own password.</p>'
        + '<div class="acct-form-actions"><button class="btn" type="submit">Add account</button></div></form>',
    ) if password_on else ""
    leftovers = ""
    if _local_data_pending() or (accounts.get_setting("local_data_adopted") or {}).get("declined") and any(accounts.local_data_counts().values()):
        counts = accounts.local_data_counts()
        leftovers = main._set_group(
            "Data from before sign-in",
            main._set_row("Shared local data", f"{_counts_sentence(counts)}, kept by the server before sign-in was on. Nobody can see it now.",
                          _post("/setup/data", '<input type="hidden" name="choice" value="keep"><button class="btn btn-sm" type="submit">Move to my account</button>',
                                confirm={"title": "Move the old data into your account?", "body": "Everything the shared local user had becomes yours, with its profiles.", "label": "Move it"})),
        )
    content = (
        main._page_head("Users", f"{len(rows)} account{'s' if len(rows) != 1 else ''}")
        + _admin_nav("users")
        + _notice(request)
        + reveal
        + _reauth_banner(user, "/admin/users")
        + main._set_group("Accounts", "".join(_account_row(user, row) for row in rows))
        + create + invites + registration + leftovers
    )
    response = _app_page(request, "Users", content, replace_url=replace_url)
    response.status_code = status_code
    return response


def _admin_nav(active: str) -> str:
    """The Admin tabs (Server, Users, Status), from admin_pages.py."""
    from . import admin_pages

    return admin_pages.nav(active)


def _reveal_link(title: str, url: str, note: str) -> str:
    return (
        f'<div class="acct-reveal" role="status"><strong>{escape(title)}</strong>'
        f'<div class="acct-secret"><code data-copy-text>{escape(url)}</code><button class="btn btn-sm" type="button" data-copy-target hidden>Copy</button></div>'
        f"<small>{escape(note)}</small></div>"
    )


def _target(account_id: str) -> dict[str, Any] | None:
    try:
        return accounts.get_account(account_id)
    except Exception:  # noqa: BLE001 - a malformed id
        return None


@router.post("/admin/users/create")
async def admin_create_user(request: Request) -> Response:
    user, refusal = _admin_action_guard(request)
    if refusal:
        return refusal
    assert user is not None
    form = await _form(request)
    try:
        role = form.get("role") if form.get("role") in accounts.ROLES else "member"
        account = accounts.create_account(form.get("username", ""), form.get("display_name", ""), role=role)
    except accounts.AccountError as exc:
        return _admin_users_page(request, user, reveal=_error(exc.message), status_code=exc.status_code, replace_url="/admin/users")
    token = secrets.token_urlsafe(32)
    accounts.create_link("reset", signin.token_hash(token), 7 * 24 * 3600, created_by=user["account_id"], account_id=account["id"])
    reveal = _reveal_link(f"Account {account['username']} created. Send them this link to choose a password:",
                          f"{get_settings().public_url}/auth/reset/{token}", "It works once and expires in 7 days. It is shown only now.")
    return _admin_users_page(request, user, reveal=reveal, replace_url="/admin/users?done=created")


@router.post("/admin/users/{account_id}/{action}")
async def admin_user_action(request: Request, account_id: str, action: str) -> Response:
    user, refusal = _admin_action_guard(request)
    if refusal:
        return refusal
    assert user is not None
    target = _target(account_id)
    if not target:
        return _redirect("/admin/users?error=not_found")
    if target["id"] == user["account_id"]:
        return _redirect("/admin/users?error=self")
    form = await _form(request)
    try:
        if action == "role":
            role = form.get("role", "")
            accounts.update_account(target["id"], role=role)
            accounts.delete_sessions(target["id"])
            return _redirect("/admin/users?done=role")
        if action == "disable":
            accounts.update_account(target["id"], disabled=True)
            return _redirect("/admin/users?done=disabled")
        if action == "enable":
            accounts.update_account(target["id"], disabled=False)
            return _redirect("/admin/users?done=enabled")
        if action == "reset-2fa":
            accounts.disable_totp(target["id"])
            return _redirect("/admin/users?done=reset_2fa")
        if action == "sign-out":
            accounts.delete_sessions(target["id"])
            return _redirect("/admin/users?done=signed_out_user")
        if action == "reset-link":
            token = secrets.token_urlsafe(32)
            accounts.create_link("reset", signin.token_hash(token), RESET_LINK_SECONDS, created_by=user["account_id"], account_id=target["id"])
            reveal = _reveal_link(f"Password reset link for {target['username']}:", f"{get_settings().public_url}/auth/reset/{token}",
                                  "It works once and expires in 24 hours. Using it signs that account out everywhere. It is shown only now.")
            return _admin_users_page(request, user, reveal=reveal, replace_url="/admin/users?done=reset_link")
        if action == "delete":
            if form.get("confirm", "").strip().lower() != target["username"].lower():
                return _redirect("/admin/users?error=confirm_name")
            result = accounts.delete_account(target["id"])
            if result:
                _m()._after_user_keys_deleted(result["user_keys"], result["downloads"])
            return _redirect("/admin/users?done=deleted")
    except accounts.AccountError as exc:
        return _redirect(f"/admin/users?error={exc.code}")
    return _redirect("/admin/users?error=not_found")


@router.post("/admin/users/registration")
async def admin_registration(request: Request) -> Response:
    user, refusal = _admin_action_guard(request)
    if refusal:
        return refusal
    form = await _form(request)
    mode = form.get("mode", "")
    if mode in accounts.REGISTRATION_MODES:
        accounts.set_setting("registration_mode", mode)
    return _redirect("/admin/users?done=registration")


@router.post("/admin/invites")
async def admin_create_invite(request: Request) -> Response:
    user, refusal = _admin_action_guard(request)
    if refusal:
        return refusal
    assert user is not None
    form = await _form(request)
    if accounts.registration_mode() == "closed":
        return _redirect("/admin/users")
    role = form.get("role") if form.get("role") in accounts.ROLES else "member"
    try:
        days = int(form.get("days") or 7)
    except ValueError:
        days = 7
    days = days if days in INVITE_DAYS else 7
    token = secrets.token_urlsafe(32)
    accounts.create_link("invite", signin.token_hash(token), days * 86400, created_by=user["account_id"], role=role,
                         note=accounts.clean_display_name(form.get("note", ""))[:60] if form.get("note") else "")
    reveal = _reveal_link("Invite link made:", _invite_link(token),
                          f"It creates one {'admin' if role == 'admin' else 'member'} account and expires in {days} day{'s' if days > 1 else ''}. It is shown only now.")
    return _admin_users_page(request, user, reveal=reveal, replace_url="/admin/users?done=invite")


@router.post("/admin/invites/{link_id}/revoke")
async def admin_revoke_invite(request: Request, link_id: str) -> Response:
    user, refusal = _admin_action_guard(request)
    if refusal:
        return refusal
    try:
        removed = accounts.revoke_link(link_id)
    except Exception:  # noqa: BLE001
        removed = False
    return _redirect("/admin/users?done=invite_revoked" if removed else "/admin/users?error=not_found")


# --- pieces main.py embeds -------------------------------------------------------------


def account_menu_rows(user: dict[str, Any]) -> str:
    """Rows for the header's account menu, above Sign out."""
    if not user.get("session_backed"):
        return ""
    main = _m()
    icon("shield"), icon("users")  # registers the two icons with the shared table
    rows = main._menu_row("shield", "Account & security", href="/account")
    if _is_admin(user):
        rows += main._menu_row("users", "Users", href="/admin/users")
    return rows


def settings_rows(user: dict[str, Any]) -> str:
    """Rows for Settings, Account: where the account pages are."""
    main = _m()
    if not user.get("session_backed"):
        return ""
    rows = main._set_row("Account & security", "Password, two-step verification, sign-in methods, devices, and deleting your account.",
                         f'<a class="btn btn-sm" href="/account">{icon("shield")}<span>Open</span></a>')
    if _is_admin(user):
        rows += main._set_row("Users", "Accounts, invite links, registration and password resets.",
                              f'<a class="btn btn-sm" href="/admin/users">{icon("users")}<span>Manage</span></a>')
    return main._set_group("Security", rows)


def startup() -> None:
    """Run once at app start, after the schema exists."""
    settings = get_settings()
    if settings.auth_mode == "authentik_oidc" or (settings.auth_mode == "accounts" and settings.auth_configured):
        try:
            created = accounts.backfill_oidc_accounts(signin.OIDC.key)
            if created:
                logger.info("created accounts for %s existing Authentik users", created)
        except Exception:  # noqa: BLE001
            logger.warning("could not create accounts for existing Authentik users", exc_info=True)
    if settings.auth_mode == "accounts" and settings.password_signin_enabled:
        signin.announce_setup_code()
