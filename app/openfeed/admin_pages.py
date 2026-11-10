"""Admin, Server: every install-wide setting in one place, next to Admin, Users and Status.

Personal settings stay in the Settings dialog. What is here changes the server for every
account: which third-party players and public instances may be offered, the server
switches for SponsorBlock, DeArrow and Return YouTube Dislike, whether members may
download and how much they may keep, the per-account rate limits and Takeout ceiling,
the playback probes and the maintenance runs.

Access: with sign-in on, only an admin (main._user_is_admin, which main.enforce_api_access
already applies to every /admin/ path and policy.ADMIN_ONLY to /api/admin/). Changes ask
for a fresh "Confirm it's you" like every other admin action (docs/accounts.md). With
sign-in off the one local user is the admin and nothing asks.

Values are stored by instance.py, over the .env defaults.
"""

from __future__ import annotations

import logging
import threading
from html import escape
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from . import accounts, instance, policy, signin
from .config import get_settings

logger = logging.getLogger("openfeed.admin")
router = APIRouter(include_in_schema=False)

# Which form fields each section's Save covers. A switch missing from a submitted form is
# off, but only for the section that was submitted.
SECTIONS: dict[str, tuple[str, ...]] = {
    "players": ("youtube_player", "allow_public_instances", "public_instances"),
    "lookups": ("sponsorblock_enabled", "dearrow_enabled", "ryd_enabled"),
    "downloads": ("members_may_download", "download_quota_mb"),
    "limits": ("limits", "takeout_max_mb"),
}

NOTICES = {
    "saved": "Saved. It applies to everyone at once.",
    "reset": "Back to the values in .env.",
    "started": "Started. It runs in the background; Status shows the result.",
}

# Maintenance runs: (label, what it does, function). Each starts on a thread and returns.
TASKS: dict[str, tuple[str, str]] = {
    "sync": ("Sync feeds now", "Fetch every followed channel's feed instead of waiting for the hourly pass."),
    "art": ("Fetch channel art", "Fill in missing avatars and banners for followed channels."),
    "discover": ("Discover related videos", "Ask Invidious for videos related to a few in the library."),
    "vectors": ("Rebuild topic vectors", "Recompute the topic vectors recommendations are ranked with."),
    "ryd": ("Fetch dislike estimates", "One Return YouTube Dislike batch for videos that have none yet."),
}


def _m() -> Any:
    from . import main

    return main


def _ap() -> Any:
    from . import account_pages

    return account_pages


# --- who may be here -----------------------------------------------------------------------


def _admin_user(request: Request) -> tuple[dict[str, Any] | None, Response | None]:
    """The admin making this request, or the response that turns them away."""
    main = _m()
    if get_settings().sign_in_enabled:
        return _ap()._require_admin(request)
    redirect = main._login_required_redirect(request)
    if redirect:
        return None, redirect
    user = main._current_user(request)
    if not main._user_is_admin(user):
        return None, main._access_denied_response(user, admin_only=True)
    return user, None


def _needs_reauth(user: dict[str, Any]) -> bool:
    return bool(get_settings().sign_in_enabled and user.get("session_backed") and not signin.reauth_fresh(user))


def _change_guard(request: Request) -> tuple[dict[str, Any] | None, Response | None]:
    user, refusal = _admin_user(request)
    if refusal:
        return None, refusal
    assert user is not None
    if _needs_reauth(user):
        return None, _ap()._reauth_redirect("/admin/server")
    return user, None


# --- shared pieces -------------------------------------------------------------------------


def nav(active: str) -> str:
    """The tabs across the Admin pages. Users exists only where accounts do."""
    tabs = [("server", "Server", "/admin/server")]
    if get_settings().sign_in_enabled:
        tabs.append(("users", "Users", "/admin/users"))
    tabs.append(("status", "Status", "/admin/status"))
    current = ' aria-current="page"'
    links = "".join(
        f'<a class="admin-tab{" is-active" if key == active else ""}" href="{href}"'
        f'{current if key == active else ""}>{escape(label)}</a>'
        for key, label, href in tabs
    )
    return f'<nav class="admin-tabs" aria-label="Admin">{links}</nav>'


def settings_rows(user: dict[str, Any]) -> str:
    """Settings, Account & security: where the server's settings went."""
    if policy.is_member(user):
        return ""
    main = _m()
    note = ("Players from other sites, server switches, downloads, limits and maintenance. "
            "They change the server for everyone.")
    return main._set_group("Server", main._set_row(
        "Admin", note, f'<a class="btn btn-sm" href="/admin/server">{main._icon("status")}<span>Open</span></a>'))


def _notice(request: Request, error: str = "") -> str:
    ap = _ap()
    done = NOTICES.get(str(request.query_params.get("done") or ""))
    out = ""
    if done:
        out += f'<p class="acct-notice" role="status">{ap.icon("check")}<span>{escape(done)}</span></p>'
    if error:
        out += ap._error(error)
    return out


def _switch(name: str, label: str, on: bool, note: str) -> str:
    return _m()._toggle(name, label, on, note, f'aria-label="{escape(label, quote=True)}"')


def _number_row(name: str, label: str, value: int, note: str, *, unit: str = "", low: int = 0, high: int = 100000) -> str:
    field_id = "admin-" + name.replace(".", "-").replace("[", "-").replace("]", "")
    unit_html = f'<span class="admin-unit">{escape(unit)}</span>' if unit else ""
    return (
        f'<div class="set-row"><div class="set-copy"><label for="{field_id}"><strong>{escape(label)}</strong></label>'
        f'<small id="{field_id}-note">{escape(note)}</small></div>'
        f'<div class="set-control admin-number"><input class="field-input" id="{field_id}" name="{escape(name, quote=True)}" '
        f'type="number" inputmode="numeric" min="{low}" max="{high}" step="1" value="{int(value)}" '
        f'aria-describedby="{field_id}-note">{unit_html}</div></div>'
    )


def _section_form(section: str, rows: str, *, foot: str = "") -> str:
    reset = (
        f'<button class="btn btn-sm btn-ghost" type="submit" name="reset" value="1" '
        f'aria-label="Use the values from .env for this section">Use .env values</button>'
    )
    return (
        f'<form method="post" action="/admin/server" class="admin-form" id="admin-{section}">'
        f'<input type="hidden" name="section" value="{section}">{rows}'
        f'<div class="admin-form-actions">{reset}<button class="btn btn-sm btn-primary" type="submit">Save</button></div>'
        f"{foot}</form>"
    )


def _group(label: str, body: str, foot: str = "") -> str:
    return f'<section class="set-group"><h2 class="set-label">{escape(label)}</h2><div class="set-panel">{body}</div>{foot}</section>'


def _foot(text: str) -> str:
    return f'<p class="set-foot">{escape(text)}</p>'


# --- the page ------------------------------------------------------------------------------


def _bytes(value: Any) -> str:
    return _m()._bytes_text(value) or "0 B"


def _storage_by_account() -> list[tuple[str, int]]:
    """(username, bytes kept) for accounts with downloads. Only what a quota needs."""
    from . import db

    try:
        names = {str(row["user_key"]): str(row["username"]) for row in accounts.list_accounts()}
    except Exception:  # noqa: BLE001
        names = {}
    try:
        with db.connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT split_part(user_key, ':p:', 1) AS account, COALESCE(SUM(size_bytes), 0) AS used "
                "FROM downloads GROUP BY 1 ORDER BY 2 DESC"
            )
            rows = list(cur.fetchall())
    except Exception:  # noqa: BLE001 - the page still renders
        return []
    return [(names.get(str(row["account"]), "Before sign-in" if row["account"] == "local" else "Removed account"), int(row["used"] or 0))
            for row in rows if int(row["used"] or 0) > 0]


def _players_section(values: dict[str, Any]) -> str:
    instances = "\n".join(values["public_instances"])
    rows = (
        _switch("youtube_player", "Allow the YouTube player", values["youtube_player"],
                "People can then opt in under Settings, Privacy, and press Play from YouTube when a video fails. "
                "Google sees their IP address and what they watch.")
        + _switch("allow_public_instances", "Let people add public Invidious instances", values["allow_public_instances"],
                  "Each person still opts in under Settings, Privacy. Whoever runs an instance sees their IP address.")
        + '<div class="set-row admin-textarea-row"><div class="set-copy"><label for="admin-public-instances"><strong>Instances you offer</strong></label>'
        '<small id="admin-public-instances-note">One address per line, such as https://yewtu.be. Offered to everyone '
        'who opts in to public instances.</small></div>'
        f'<textarea class="field-input admin-textarea" id="admin-public-instances" name="public_instances" rows="3" '
        f'placeholder="https://yewtu.be" aria-describedby="admin-public-instances-note" spellcheck="false">{escape(instances)}</textarea></div>'
    )
    return _group("Players from other sites", _section_form("players", rows),
                  _foot("Off by default. Nothing ever switches to these by itself (PRIVACY.md)."))


def _lookups_section(values: dict[str, Any]) -> str:
    rows = (
        _switch("sponsorblock_enabled", "SponsorBlock", values["sponsorblock_enabled"],
                "Skip segments. Sends a 4-character hash prefix of a video id to sponsor.ajay.app.")
        + _switch("dearrow_enabled", "DeArrow", values["dearrow_enabled"],
                  "Community titles and thumbnails. Same hash prefix, same service.")
        + _switch("ryd_enabled", "Return YouTube Dislike", values["ryd_enabled"],
                  "Dislike estimates. Sends full video ids, mixed into batches, to returnyoutubedislikeapi.com.")
    )
    return _group("Lookups this server makes", _section_form("lookups", rows),
                  _foot("Off here turns one off for everyone. On lets each person opt in under Settings, Integrations; "
                        "nothing is fetched for someone who has not."))


def _playback_section() -> str:
    main = _m()
    settings = get_settings()
    from . import ytdlp

    rows = (
        main._set_row("Invidious", settings.invidious_public_url or "Not configured")
        + main._set_row("yt-dlp fallback", "Available" if settings.ytdlp_enabled and ytdlp.available() else "Not available")
        + '<div class="set-row"><div class="set-copy"><strong>Test all sources</strong>'
        "<small>Ask every source for a real player, one at a time.</small></div>"
        '<div class="set-control"><button class="btn btn-sm" type="button" data-admin-test>Test all sources</button></div></div>'
        '<div class="set-row"><div class="set-copy"><strong>Diagnose playback</strong>'
        "<small>Walk the private playback path hop by hop and name the first break.</small></div>"
        '<div class="set-control"><button class="btn btn-sm" type="button" data-admin-diagnose>Diagnose</button></div></div>'
        '<div class="admin-results" data-admin-results role="status" aria-live="polite"></div>'
    )
    return _group("Playback sources", rows,
                  _foot("Each person orders and switches off sources for themselves under Settings, Playback. "
                        "These probes run from this server."))


def _downloads_section(values: dict[str, Any]) -> str:
    main = _m()
    from . import downloads

    try:
        usage = downloads.disk_usage()
    except OSError:
        usage = {}
    free = usage.get("free_bytes")
    where = (f"{usage.get('path') or get_settings().download_dir}: {_bytes(usage.get('bytes'))} in "
             f"{int(usage.get('files') or 0)} files" + (f", {_bytes(free)} free" if free is not None else ""))
    rows = (
        _switch("members_may_download", "Members may download", values["members_may_download"],
                "Off hides Download for members and refuses the API. Admins can always download.")
        + _number_row("download_quota_mb", "Storage per member", values["download_quota_mb"],
                      "For videos kept on the server, counted per account. 0 means no limit.", unit="MB", high=instance.QUOTA_MB_CEILING)
    )
    shared = get_settings().sign_in_enabled
    usage_rows = ""
    if shared:
        per_account = _storage_by_account()
        usage_rows = "".join(main._set_row(name, control=f"<span>{escape(_bytes(used))}</span>") for name, used in per_account) \
            or main._set_row("Nobody keeps downloads on the server yet.")
    body = (
        _section_form("downloads", rows)
        + main._set_row("Location", where + ". Set OPENFEED_DOWNLOAD_DIR to move it.")
    )
    extra = _group("Kept downloads by account", usage_rows,
                   _foot("Sizes only, for quotas. Which videos someone kept is theirs.")) if shared else ""
    return _group("Downloads", body, _foot(
        "Identical files kept by several people are stored once; each person is charged the full size, and deleting "
        "one person's copy never removes another's.")) + extra


def _limits_section(values: dict[str, Any]) -> str:
    limits = values["limits"]
    rows = "".join(
        _number_row(f"limits.{limit.key}", limit.label, int(limits.get(limit.key, limit.default)),
                    f"Per account, {limit.window_label}. Default {limit.default}; 0 turns this limit off.", high=instance.LIMIT_CEILING)
        for limit in instance.LIMITS
    ) + _number_row("takeout_max_mb", "Largest Takeout upload", values["takeout_max_mb"],
                    "Bigger archives are refused before they are read.", unit="MB", low=1, high=instance.TAKEOUT_MAX_MB_CEILING)
    note = ("Each account's profiles and API tokens share its counters. Admins are not limited."
            if get_settings().sign_in_enabled else
            "Sign-in is off, so there is one user and nothing is rate-limited. These apply once sign-in is on.")
    return _group("Limits", _section_form("limits", rows), _foot(note))


def _maintenance_section() -> str:
    rows = "".join(
        f'<div class="set-row"><div class="set-copy"><strong>{escape(label)}</strong><small>{escape(note)}</small></div>'
        f'<div class="set-control"><form method="post" action="/admin/server/run/{key}">'
        f'<button class="btn btn-sm" type="submit">Run</button></form></div></div>'
        for key, (label, note) in TASKS.items()
        if key != "ryd" or get_settings().ryd_enabled
    )
    return _group("Maintenance", rows, _foot("The worker runs all of these on its own schedule. These start one now."))


def server_page(request: Request, user: dict[str, Any], *, error: str = "", status_code: int = 200) -> Response:
    main = _m()
    values = instance.current()
    lock = _ap()._reauth_banner(user, "/admin/server") if get_settings().sign_in_enabled and user.get("session_backed") else ""
    registration = ""
    if get_settings().sign_in_enabled:
        registration = _group("Registration", main._set_row(
            "Who can create an account", f"Now: {accounts.registration_mode()}. Invite links and open sign-up are under Users.",
            '<a class="btn btn-sm" href="/admin/users">Users</a>'))
    content = (
        main._page_head("Admin", "Changes here apply to everyone")
        + nav("server")
        + _notice(request, error)
        + lock
        + _players_section(values)
        + _lookups_section(values)
        + _playback_section()
        + _downloads_section(values)
        + _limits_section(values)
        + registration
        + _maintenance_section()
    )
    # An error is drawn as the answer to the form's POST: the address bar goes back to the page.
    response = _ap()._app_page(request, "Admin", f'<div class="admin-page" data-admin-page>{content}</div>',
                               replace_url="/admin/server" if request.method == "POST" else "")
    response.status_code = status_code
    return response


@router.get("/admin/server")
def admin_server(request: Request) -> Response:
    user, refusal = _admin_user(request)
    if refusal:
        return refusal
    assert user is not None
    return server_page(request, user)


def _changes_from_form(section: str, form: dict[str, str]) -> dict[str, Any]:
    fields = SECTIONS[section]
    changes: dict[str, Any] = {}
    for field in fields:
        kind = instance.FIELDS[field]
        if field == "limits":
            changes["limits"] = {limit.key: form.get(f"limits.{limit.key}", "") for limit in instance.LIMITS
                                 if f"limits.{limit.key}" in form}
        elif kind is bool:
            changes[field] = form.get(field) in {"on", "true", "1"}
        elif field in form:
            changes[field] = form[field]
    return changes


@router.post("/admin/server")
async def admin_server_save(request: Request) -> Response:
    user, refusal = _change_guard(request)
    if refusal:
        return refusal
    assert user is not None
    form = await _ap()._form(request)
    section = form.get("section", "")
    if section not in SECTIONS:
        return server_page(request, user, error="That form is out of date. Reload the page and try again.", status_code=400)
    try:
        if form.get("reset"):
            instance.reset(list(SECTIONS[section]))
            return _ap()._redirect(f"/admin/server?done=reset#admin-{section}")
        instance.save(_changes_from_form(section, form))
    except instance.InvalidSetting as exc:
        return server_page(request, user, error=exc.message, status_code=400)
    logger.info("admin changed server settings: %s", section)
    return _ap()._redirect(f"/admin/server?done=saved#admin-{section}")


def _run_task(key: str) -> None:
    try:
        if key == "sync":
            from .rss import sync_rss_once

            sync_rss_once(get_settings().rss_poll_limit)
        elif key == "art":
            from .enrichment import backfill_subscription_art

            backfill_subscription_art()
        elif key == "discover":
            from .discovery import discover_related_videos

            discover_related_videos(3, 6)
        elif key == "vectors":
            from . import topics

            topics.clear_cache()
            topics.rebuild_vectors(2000)
        elif key == "ryd":
            from . import ryd

            ryd.refresh_missing(200)
    except Exception as exc:  # noqa: BLE001 - logged; Status shows the outcome
        logger.warning("admin task %s failed: %s", key, exc)


@router.post("/admin/server/run/{task}")
async def admin_run_task(request: Request, task: str) -> Response:
    user, refusal = _change_guard(request)
    if refusal:
        return refusal
    if task not in TASKS:
        return _ap()._redirect("/admin/server")
    threading.Thread(target=_run_task, args=(task,), name=f"admin-{task}", daemon=True).start()
    return _ap()._redirect("/admin/server?done=started")


# --- JSON, for scripts with an admin token -------------------------------------------------


def _settings_payload() -> dict[str, Any]:
    return {
        "ok": True,
        "settings": instance.current(),
        "saved": instance.stored(),
        "defaults": instance.env_defaults(),
        "limits": [{"key": limit.key, "label": limit.label, "window_seconds": limit.window, "default": limit.default}
                   for limit in instance.LIMITS],
    }


def _api_admin(request: Request) -> dict[str, Any] | None:
    """The admin behind an API call (a session or an admin's token), or None."""
    main = _m()
    user = main._current_user(request)
    if get_settings().require_login and not user.get("authenticated"):
        return None
    return user if main._user_is_admin(user) else None


async def api_admin_settings(request: Request) -> Response:
    if _api_admin(request) is None:
        return JSONResponse({"detail": "admin_required"}, status_code=403)
    return JSONResponse(_settings_payload())


async def api_admin_settings_update(request: Request) -> Response:
    user = _api_admin(request)
    if user is None:
        return JSONResponse({"detail": "admin_required"}, status_code=403)
    if _needs_reauth(user):
        return JSONResponse({"detail": "reauth_required", "message": "Confirm it's you first (/auth/confirm)."}, status_code=403)
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"detail": "Send a JSON object of settings."}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"detail": "Send a JSON object of settings."}, status_code=400)
    try:
        reset = payload.pop("reset", None)
        if reset:
            instance.reset([str(key) for key in reset] if isinstance(reset, list) else list(instance.FIELDS))
        if payload:
            instance.save(payload)
    except instance.InvalidSetting as exc:
        return JSONResponse({"detail": exc.message, "field": exc.field}, status_code=400)
    return JSONResponse(_settings_payload())


def install(app: Any) -> None:
    """Add these routes to the app (plain routes, as main does for account_pages)."""
    app.router.routes.extend(router.routes)
    app.add_api_route("/api/admin/settings", api_admin_settings, methods=["GET"], tags=["admin"],
                      summary="Install-wide settings (admin)")
    app.add_api_route("/api/admin/settings", api_admin_settings_update, methods=["PUT", "PATCH"], tags=["admin"],
                      summary="Change install-wide settings (admin)")


__all__ = ["SECTIONS", "TASKS", "install", "nav", "router", "server_page", "settings_rows"]
