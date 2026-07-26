from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable, TypeVar

T = TypeVar("T")


def _env(reader: Callable[[], T]) -> T:
    """Read an environment variable when Settings is built, not when this module loads.

    Evaluating os.getenv in the field default would freeze the value at import time,
    which makes every setting untestable and silently ignores anything the process
    learns about its environment later.
    """
    return field(default_factory=reader)  # type: ignore[return-value]


def _str_env(name: str, default: str = "", *, strip_slash: bool = False, lower: bool = False) -> str:
    value = (os.getenv(name) or default).strip()
    if strip_slash:
        value = value.rstrip("/")
    return value.lower() if lower else value


def _first_env(*names: str, default: str = "", strip_slash: bool = False) -> str:
    for name in names:
        value = (os.getenv(name) or "").strip()
        if value:
            return value.rstrip("/") if strip_slash else value
    return default.rstrip("/") if strip_slash else default


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _set_env(name: str) -> frozenset[str]:
    return frozenset(value.strip() for value in os.getenv(name, "").split(",") if value.strip())


@dataclass(frozen=True)
class Settings:
    public_url: str = _env(lambda: _str_env("OPENFEED_PUBLIC_URL", "http://localhost:8060", strip_slash=True))
    # AGPL-3.0 section 13: people using this over a network are entitled to the source
    # of the version they are using. Point this at your fork if you have modified it.
    source_url: str = _env(lambda: _str_env("OPENFEED_SOURCE_URL", "https://github.com/S0nrxse/openFeed", strip_slash=True))
    secret_key: str = _env(lambda: _str_env("OPENFEED_SECRET_KEY"))
    database_url: str = _env(lambda: _str_env("DATABASE_URL"))
    redis_url: str = _env(lambda: _str_env("REDIS_URL", "redis://openfeed-redis:6379/0"))
    gorse_base_url: str = _env(lambda: _str_env("GORSE_BASE_URL", "http://openfeed-gorse:8088", strip_slash=True))
    gorse_server_api_key: str = _env(lambda: _str_env("GORSE_SERVER_API_KEY"))
    freshrss_base_url: str = _env(lambda: _str_env("FRESHRSS_BASE_URL", "http://freshrss", strip_slash=True))
    freshrss_username: str = _env(lambda: _str_env("FRESHRSS_USERNAME"))
    freshrss_api_password: str = _env(lambda: _str_env("FRESHRSS_API_PASSWORD"))
    freshrss_reading_list_limit: int = _env(lambda: _int_env("FRESHRSS_READING_LIST_LIMIT", 250))
    sync_interval_seconds: int = _env(lambda: _int_env("SYNC_INTERVAL_SECONDS", 1800))
    enrich_after_sync_limit: int = _env(lambda: _int_env("ENRICH_AFTER_SYNC_LIMIT", 5))
    enrichment_timeout_seconds: int = _env(lambda: _int_env("ENRICHMENT_TIMEOUT_SECONDS", 12))
    invidious_base_url: str = _env(lambda: _str_env("INVIDIOUS_BASE_URL", "http://openfeed-invidious:3000", strip_slash=True))
    invidious_public_url: str = _env(lambda: _str_env("INVIDIOUS_PUBLIC_URL", "http://localhost:8061", strip_slash=True))
    # Playback fallback ladder. The local Invidious is always tried first; these decide
    # what happens when its companion cannot get a PO token and YouTube refuses streams.
    playback_fallback: str = _env(lambda: _str_env("OPENFEED_PLAYBACK_FALLBACK", "youtube", lower=True))
    invidious_fallback_urls: str = _env(lambda: _str_env("OPENFEED_INVIDIOUS_FALLBACKS"))
    playback_probe_timeout_seconds: float = _env(lambda: float(_int_env("OPENFEED_PLAYBACK_PROBE_TIMEOUT", 4)))
    discover_after_sync_limit: int = _env(lambda: _int_env("DISCOVER_AFTER_SYNC_LIMIT", 3))
    discover_related_per_video: int = _env(lambda: _int_env("DISCOVER_RELATED_PER_VIDEO", 6))
    auth_mode: str = _env(lambda: _str_env("OPENFEED_AUTH_MODE", "disabled", lower=True))
    require_login: bool = _env(lambda: _bool_env("OPENFEED_REQUIRE_LOGIN", False))
    required_group: str = _env(lambda: _str_env("OPENFEED_REQUIRED_GROUP"))
    admin_groups: frozenset[str] = _env(lambda: _set_env("OPENFEED_ADMIN_GROUPS"))
    session_secret: str = _env(lambda: _first_env("OPENFEED_SESSION_SECRET", "OPENFEED_SECRET_KEY"))
    session_cookie: str = _env(lambda: _str_env("OPENFEED_SESSION_COOKIE", "openfeed_session"))
    oidc_state_cookie: str = _env(lambda: _str_env("OPENFEED_OIDC_STATE_COOKIE", "openfeed_oidc_state"))
    rss_guid_namespace: str = _env(lambda: _str_env("OPENFEED_RSS_GUID_NAMESPACE", "openfeed") or "openfeed")
    oidc_issuer: str = _env(lambda: _first_env("OPENFEED_OIDC_ISSUER", "AUTHENTIK_OIDC_ISSUER", strip_slash=True))
    oidc_discovery_url: str = _env(lambda: _str_env("OPENFEED_OIDC_DISCOVERY_URL"))
    oidc_backchannel_base_url: str = _env(lambda: _first_env("OPENFEED_OIDC_BACKCHANNEL_BASE_URL", "AUTHENTIK_OIDC_BACKCHANNEL_BASE_URL", strip_slash=True))
    oidc_client_id: str = _env(lambda: _first_env("OPENFEED_OIDC_CLIENT_ID", "AUTHENTIK_OIDC_CLIENT_ID"))
    oidc_client_secret: str = _env(lambda: _first_env("OPENFEED_OIDC_CLIENT_SECRET", "AUTHENTIK_OIDC_CLIENT_SECRET"))
    oidc_redirect_uri: str = _env(lambda: _str_env("OPENFEED_OIDC_REDIRECT_URI"))
    oidc_scope: str = _env(lambda: _str_env("OPENFEED_OIDC_SCOPE", "openid email profile"))

    @property
    def auth_configured(self) -> bool:
        return bool(self.session_secret and self.oidc_issuer and self.oidc_client_id and self.oidc_client_secret)

    @property
    def oidc_public_issuer(self) -> str:
        return f"{self.oidc_issuer}/" if self.oidc_issuer else ""

    @property
    def freshrss_configured(self) -> bool:
        return bool(self.freshrss_base_url and self.freshrss_username and self.freshrss_api_password)

    @property
    def youtube_fallback_enabled(self) -> bool:
        return self.playback_fallback not in {"off", "none", "disabled", "0", "false", "no"}

    @property
    def invidious_fallback_list(self) -> list[str]:
        return [url.strip().rstrip("/") for url in self.invidious_fallback_urls.split(",") if url.strip()]


def get_settings() -> Settings:
    return Settings()
