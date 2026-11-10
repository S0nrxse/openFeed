"""Install-wide settings an admin can change in the app, and who counts as an admin.

Everything else in Settings is personal: it is stored per profile and changes nothing for
anyone else. What is here changes the server for everyone, so with sign-in on only an
admin can read or change it (main.enforce_api_access and admin_pages.py), and with sign-in
off the one local user is the admin.

Values come from the environment first (config.Settings), and what an admin saves in
Admin, Server overrides them. The saved values live in one app_settings row
(`instance`), so an install that never touched them behaves exactly as its .env says.

Reading happens on every request, so the saved row is cached in this process for a few
seconds and re-read after that; a save clears the cache in the saving process at once and
reaches the others (worker, other web processes) within CACHE_SECONDS.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("openfeed.instance")

SETTING_KEY = "instance"
CACHE_SECONDS = 10.0
# After a failed read the cache keeps the environment's answer this long, so a database
# outage costs one slow connect per minute rather than one per request.
FAILURE_SECONDS = 60.0


@dataclass(frozen=True)
class Limit:
    """One per-account rate limit: at most `default` requests per `window` seconds."""

    key: str
    label: str
    window: int
    default: int
    # The sentence a refused request gets, before "Try again in ...".
    message: str
    window_label: str


# Rate limits for a public instance. Each account (all its profiles together) gets its own
# counters, admins are exempt, and with sign-in off nothing is limited. 0 turns one off.
LIMITS: tuple[Limit, ...] = (
    Limit("search", "Searches", 10 * 60, 120,
          "You've searched a lot in the last 10 minutes.", "per 10 minutes"),
    Limit("invidious", "Channel, playlist and comment pages", 10 * 60, 600,
          "You've opened a lot of channel, playlist and video pages in the last 10 minutes.", "per 10 minutes"),
    Limit("playlist_import", "Playlist imports and saves", 60 * 60, 10,
          "You've imported a lot of playlists in the last hour.", "per hour"),
    Limit("takeout_import", "Takeout imports", 24 * 60 * 60, 5,
          "You've imported several Takeout archives today.", "per day"),
    Limit("downloads", "Downloads started", 60 * 60, 30,
          "You've started a lot of downloads in the last hour.", "per hour"),
    Limit("opml_import", "OPML imports", 60 * 60, 10,
          "You've imported a lot of subscription lists in the last hour.", "per hour"),
)
LIMITS_BY_KEY = {limit.key: limit for limit in LIMITS}

# The values Admin, Server can change, with their type. Each has an environment default
# (env_defaults below); the stored row only ever holds keys from this table.
FIELDS: dict[str, type] = {
    # Third-party players and public instances (privacy.py reads these through Settings).
    "youtube_player": bool,
    "allow_public_instances": bool,
    "public_instances": list,
    # Server switches for the lookups the server makes on a user's behalf.
    "ryd_enabled": bool,
    "sponsorblock_enabled": bool,
    "dearrow_enabled": bool,
    # Downloads.
    "members_may_download": bool,
    "download_quota_mb": int,
    # Takeout uploads, in MB.
    "takeout_max_mb": int,
    # Per-account rate limits: {limit key: requests per window}.
    "limits": dict,
}

TAKEOUT_MAX_MB_CEILING = 2048  # takeout.MAX_ARCHIVE_BYTES, the parser's own hard limit
QUOTA_MB_CEILING = 10 * 1024 * 1024  # 10 TB; anything above is a typo
LIMIT_CEILING = 100000

_lock = threading.Lock()
_cache: dict[str, Any] = {"at": 0.0, "ttl": 0.0, "stored": {}}


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name) or "").strip() or default)
    except ValueError:
        return default


def env_defaults() -> dict[str, Any]:
    """What the environment says, before anything saved in the app."""
    fallback = (os.getenv("OPENFEED_PLAYBACK_FALLBACK") or "off").strip().lower()
    return {
        "youtube_player": fallback not in {"off", "none", "disabled", "0", "false", "no", ""},
        "allow_public_instances": _bool_env("OPENFEED_ALLOW_PUBLIC_INSTANCES", False),
        "public_instances": [url.strip().rstrip("/") for url in (os.getenv("OPENFEED_INVIDIOUS_FALLBACKS") or "").split(",") if url.strip()],
        "ryd_enabled": _bool_env("OPENFEED_RYD_ENABLED", True),
        "sponsorblock_enabled": _bool_env("OPENFEED_SPONSORBLOCK_ENABLED", True),
        "dearrow_enabled": _bool_env("OPENFEED_DEARROW_ENABLED", True),
        "members_may_download": _bool_env("OPENFEED_MEMBERS_MAY_DOWNLOAD", True),
        "download_quota_mb": max(0, _int_env("OPENFEED_DOWNLOAD_QUOTA_MB", 0)),
        "takeout_max_mb": max(1, min(TAKEOUT_MAX_MB_CEILING, _int_env("OPENFEED_TAKEOUT_MAX_MB", TAKEOUT_MAX_MB_CEILING))),
        "limits": {limit.key: max(0, _int_env(f"OPENFEED_RATE_LIMIT_{limit.key.upper()}", limit.default)) for limit in LIMITS},
    }


# --- the stored row ------------------------------------------------------------------------


def _read_stored() -> dict[str, Any]:
    """The saved row, read on its own short connection.

    Not through db.connect(): this runs from config.get_settings(), which nearly every
    function calls, and tests replace db.connect with fakes that count their calls. Without
    a DATABASE_URL (unit tests, a fresh checkout) there is nothing saved.
    """
    url = (os.getenv("DATABASE_URL") or "").strip()
    if not url:
        return {}
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(url, row_factory=dict_row, connect_timeout=3) as conn:
        row = conn.execute("SELECT value FROM app_settings WHERE key = %s", (SETTING_KEY,)).fetchone()
    value = (row or {}).get("value")
    return clean(value) if isinstance(value, dict) else {}


def stored() -> dict[str, Any]:
    """The values an admin saved, cached for CACHE_SECONDS. Never raises."""
    now = time.monotonic()
    with _lock:
        if now - _cache["at"] < _cache["ttl"]:
            return dict(_cache["stored"])
    try:
        value = _read_stored()
        ttl = CACHE_SECONDS
    except Exception as exc:  # noqa: BLE001 - the environment's answer stands
        logger.info("install settings unreadable, using the environment: %s", exc.__class__.__name__)
        value, ttl = {}, FAILURE_SECONDS
    with _lock:
        _cache.update(at=time.monotonic(), ttl=ttl, stored=dict(value))
    return dict(value)


def forget() -> None:
    """Drop the cache, so the next read goes to the database (after a save, and in tests)."""
    with _lock:
        _cache.update(at=0.0, ttl=0.0, stored={})


def prime(values: dict[str, Any]) -> None:
    """Put values in the cache as if they had just been read. For tests and after a save."""
    with _lock:
        _cache.update(at=time.monotonic(), ttl=CACHE_SECONDS, stored=clean(values))


def current() -> dict[str, Any]:
    """Every field: the environment's value, replaced by what an admin saved."""
    values = env_defaults()
    saved = stored()
    for key, value in saved.items():
        if key == "limits":
            values["limits"] = {**values["limits"], **value}
        else:
            values[key] = value
    return values


def value(key: str) -> Any:
    return current()[key]


# --- validation ----------------------------------------------------------------------------


class InvalidSetting(ValueError):
    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field
        self.message = message


def _as_bool(raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _as_int(field: str, raw: Any, low: int, high: int) -> int:
    try:
        number = int(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise InvalidSetting(field, "Enter a whole number.") from exc
    if number < low or number > high:
        raise InvalidSetting(field, f"Enter a number from {low} to {high}.")
    return number


def _as_instances(raw: Any) -> list[str]:
    from . import db

    items = raw if isinstance(raw, list) else str(raw or "").replace("\n", ",").split(",")
    urls: list[str] = []
    for item in items:
        text = str(item or "").strip().rstrip("/")
        if not text:
            continue
        if "://" not in text:
            text = "https://" + text
        problem = db.instance_url_problem(text)
        if problem:
            raise InvalidSetting("public_instances", f"{text} {problem}.")
        if text not in urls:
            urls.append(text)
    if len(urls) > 12:
        raise InvalidSetting("public_instances", "List at most 12 instances.")
    return urls


def validate(changes: dict[str, Any]) -> dict[str, Any]:
    """Typed, bounded values for the fields given. Unknown keys are refused, not dropped."""
    out: dict[str, Any] = {}
    for key, raw in changes.items():
        if key not in FIELDS:
            raise InvalidSetting(key, f"{key} is not a server setting.")
        kind = FIELDS[key]
        if kind is bool:
            out[key] = _as_bool(raw)
        elif key == "download_quota_mb":
            out[key] = _as_int(key, raw, 0, QUOTA_MB_CEILING)
        elif key == "takeout_max_mb":
            out[key] = _as_int(key, raw, 1, TAKEOUT_MAX_MB_CEILING)
        elif key == "public_instances":
            out[key] = _as_instances(raw)
        elif key == "limits":
            if not isinstance(raw, dict):
                raise InvalidSetting(key, "limits must map a limit to a number.")
            limits: dict[str, int] = {}
            for name, count in raw.items():
                if name not in LIMITS_BY_KEY:
                    raise InvalidSetting(f"limits.{name}", f"{name} is not a rate limit.")
                limits[name] = _as_int(f"limits.{name}", count, 0, LIMIT_CEILING)
            out[key] = limits
    return out


def clean(raw: dict[str, Any]) -> dict[str, Any]:
    """The valid part of a stored row. A bad value saved by an older version is skipped."""
    out: dict[str, Any] = {}
    for key, value in (raw or {}).items():
        if key not in FIELDS:
            continue
        try:
            if key == "public_instances":
                # Stored already validated; re-checking would need DNS on every read.
                out[key] = [str(url) for url in value if isinstance(url, str)][:12] if isinstance(value, list) else []
            else:
                out.update(validate({key: value}))
        except InvalidSetting:
            continue
    return out


def save(changes: dict[str, Any]) -> dict[str, Any]:
    """Validate and store changes (merged into what is saved). Returns current()."""
    from . import accounts

    valid = validate(changes)
    merged = stored()
    for key, value in valid.items():
        if key == "limits":
            merged["limits"] = {**(merged.get("limits") or {}), **value}
        else:
            merged[key] = value
    accounts.set_setting(SETTING_KEY, merged)
    prime(merged)
    return current()


def reset(keys: list[str]) -> dict[str, Any]:
    """Go back to the environment's value for these fields."""
    from . import accounts

    merged = {key: value for key, value in stored().items() if key not in set(keys)}
    accounts.set_setting(SETTING_KEY, merged)
    prime(merged)
    return current()


# --- what config.get_settings() sees -------------------------------------------------------


def settings_overrides() -> dict[str, Any]:
    """Settings fields to replace, from what an admin saved. Empty when nothing was saved."""
    saved = stored()
    if not saved:
        return {}
    out: dict[str, Any] = {}
    for key in ("ryd_enabled", "sponsorblock_enabled", "dearrow_enabled", "allow_public_instances"):
        if key in saved:
            out[key] = bool(saved[key])
    if "youtube_player" in saved:
        out["playback_fallback"] = "youtube" if saved["youtube_player"] else "off"
    if "public_instances" in saved:
        out["invidious_fallback_urls"] = ",".join(saved["public_instances"])
    return out


# --- convenience readers -------------------------------------------------------------------


def members_may_download() -> bool:
    return bool(value("members_may_download"))


def download_quota_bytes() -> int:
    """Per-account storage for downloads kept on the server. 0 means no limit."""
    return int(value("download_quota_mb") or 0) * 1024 * 1024


def takeout_max_bytes() -> int:
    return int(value("takeout_max_mb") or TAKEOUT_MAX_MB_CEILING) * 1024 * 1024


def limit_count(key: str) -> int:
    return int((value("limits") or {}).get(key, LIMITS_BY_KEY[key].default))


__all__ = [
    "FIELDS", "LIMITS", "LIMITS_BY_KEY", "InvalidSetting", "current", "env_defaults", "forget", "prime",
    "save", "reset", "settings_overrides", "stored", "validate", "value",
]
