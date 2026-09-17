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
    # Direct YouTube RSS polling of subscribed channels (no FreshRSS involved).
    rss_poll_limit: int = _env(lambda: _int_env("RSS_POLL_LIMIT", 100))
    rss_poll_ttl_seconds: int = _env(lambda: _int_env("RSS_POLL_TTL_SECONDS", 3600))
    sync_interval_seconds: int = _env(lambda: _int_env("SYNC_INTERVAL_SECONDS", 1800))
    # Discovered videos arrive with almost no metadata — no tags, usually no publish
    # date — and topic-based ranking cannot see a video whose topics were never
    # fetched. At 5 per sync the backlog grew faster than it drained, which is why the
    # feed kept falling back on the handful of well-described subscription channels.
    enrich_after_sync_limit: int = _env(lambda: _int_env("ENRICH_AFTER_SYNC_LIMIT", 40))
    enrichment_timeout_seconds: int = _env(lambda: _int_env("ENRICHMENT_TIMEOUT_SECONDS", 12))
    invidious_base_url: str = _env(lambda: _str_env("INVIDIOUS_BASE_URL", "http://openfeed-invidious:3000", strip_slash=True))
    invidious_public_url: str = _env(lambda: _str_env("INVIDIOUS_PUBLIC_URL", "http://localhost:8061", strip_slash=True))
    # Playback fallback ladder. The local Invidious is always tried first; these decide
    # what happens when its companion cannot get a PO token and YouTube refuses streams.
    playback_fallback: str = _env(lambda: _str_env("OPENFEED_PLAYBACK_FALLBACK", "youtube", lower=True))
    invidious_fallback_urls: str = _env(lambda: _str_env("OPENFEED_INVIDIOUS_FALLBACKS"))
    playback_probe_timeout_seconds: float = _env(lambda: float(_int_env("OPENFEED_PLAYBACK_PROBE_TIMEOUT", 4)))
    # yt-dlp fallback media backend. Invidious stays primary for browsing, search,
    # channels, comments, recommendations and normal playback; yt-dlp is reached for only
    # when Invidious cannot produce a usable stream, or when a download needs a rendition
    # that YouTube only publishes as separate video and audio tracks.
    ytdlp_enabled: bool = _env(lambda: _bool_env("OPENFEED_YTDLP_ENABLED", True))
    ytdlp_binary: str = _env(lambda: _str_env("OPENFEED_YTDLP_BINARY", "yt-dlp"))
    ytdlp_timeout_seconds: int = _env(lambda: _int_env("OPENFEED_YTDLP_TIMEOUT", 90))
    # Extracted URLs are signed and short-lived. This cache exists only so one page load
    # cannot spawn several extractions — never to hold a URL long enough to expire.
    ytdlp_cache_seconds: int = _env(lambda: _int_env("OPENFEED_YTDLP_CACHE_SECONDS", 240))
    # Path to a Netscape cookies file, if YouTube demands sign-in for some videos. Supply
    # the file yourself; its contents are a credential and are never logged.
    ytdlp_cookies_file: str = _env(lambda: _str_env("OPENFEED_YTDLP_COOKIES_FILE"))
    # Escape hatch for the extractor flags that YouTube breakage usually needs, e.g.
    # --extractor-args youtube:player_client=web_safari
    ytdlp_extra_args: str = _env(lambda: _str_env("OPENFEED_YTDLP_EXTRA_ARGS"))
    ytdlp_playback_fallback: bool = _env(lambda: _bool_env("OPENFEED_YTDLP_PLAYBACK", True))
    # A real folder, not a temp dir: downloads kept on the server are a library and
    # have to survive a container restart. Bind-mounted in compose.
    download_dir: str = _env(lambda: _str_env("OPENFEED_DOWNLOAD_DIR", "/data/downloads"))
    download_retention_seconds: int = _env(lambda: _int_env("OPENFEED_DOWNLOAD_RETENTION", 21600))
    download_max_concurrent: int = _env(lambda: _int_env("OPENFEED_DOWNLOAD_CONCURRENCY", 2))
    download_max_height: int = _env(lambda: _int_env("OPENFEED_DOWNLOAD_MAX_HEIGHT", 2160))
    # Return YouTube Dislike. This is the deployment-level switch; each user still has to
    # opt in individually in Settings, and both must be true before anything is fetched.
    # Set false to make the feature unavailable no matter what a user turns on.
    ryd_enabled: bool = _env(lambda: _bool_env("OPENFEED_RYD_ENABLED", True))
    ryd_batch_size: int = _env(lambda: _int_env("OPENFEED_RYD_BATCH_SIZE", 40))
    # SponsorBlock. Deployment-level switch; each user still opts in separately, and both
    # must be true before a single request leaves the server.
    sponsorblock_enabled: bool = _env(lambda: _bool_env("OPENFEED_SPONSORBLOCK_ENABLED", True))
    # Segments rarely change, so this can be long — but not unbounded, because a segment
    # can be corrected or removed by the community after we cached it.
    sponsorblock_cache_seconds: int = _env(lambda: _int_env("OPENFEED_SPONSORBLOCK_CACHE_SECONDS", 21600))
    # Community posts. Cached because a channel page and both feeds all read them, and a
    # channel posts a few times a week at most.
    posts_cache_seconds: int = _env(lambda: _int_env("OPENFEED_POSTS_CACHE_SECONDS", 1800))
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
    def youtube_fallback_enabled(self) -> bool:
        return self.playback_fallback not in {"off", "none", "disabled", "0", "false", "no"}

    @property
    def invidious_fallback_list(self) -> list[str]:
        return [url.strip().rstrip("/") for url in self.invidious_fallback_urls.split(",") if url.strip()]


def get_settings() -> Settings:
    return Settings()
