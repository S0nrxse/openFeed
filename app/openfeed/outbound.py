"""Every server-side request to YouTube or another third party leaves through here.

OpenFeed talks to three kinds of hosts:

- its own Invidious (and companion), which is part of the stack and is reached directly;
- YouTube: RSS feeds, the oEmbed fallback, yt-dlp extraction, downloads and live chat;
- opt-in third parties: Return YouTube Dislike, SponsorBlock, DeArrow, public Invidious
  instances and the YouTube embed probe.

The last two groups see this server's address. `OPENFEED_OUTBOUND_PROXY` sends all of them
through a proxy instead (an http://, https:// or socks5:// URL, e.g. a Tor or VPN gateway),
so the address they see is the proxy's. Calls to the deployment's own Invidious never use it:
Invidious makes its own YouTube requests and has its own proxy setting.

yt-dlp gets the same proxy through `--proxy`. That matters twice over: a signed stream URL is
bound to the address that extracted it, so the stream proxy in main.py has to fetch through the
same exit as yt-dlp did, which `media_client()` does.
"""

from __future__ import annotations

import os
import threading
import urllib.parse
from typing import Any

import httpx

# Sent instead of a browser user agent. It names the software, not the person.
USER_AGENT = "OpenFeed (+https://github.com/S0nrxse/openFeed)"


def proxy_url() -> str:
    """The configured outbound proxy, or "" for direct connections.

    Read on each call rather than at import, so tests and a changed environment see it.
    A value that is not an http, https, socks5 or socks5h URL is ignored rather than
    half-applied: sending traffic direct while the operator thinks it is proxied would be
    worse than failing loudly, so `proxy_problem()` reports it on the status page.
    """
    raw = (os.getenv("OPENFEED_OUTBOUND_PROXY") or "").strip()
    return raw if raw and not proxy_problem(raw) else ""


def proxy_problem(raw: str | None = None) -> str:
    """Why the configured proxy is unusable, or "" when it is fine or unset."""
    value = (os.getenv("OPENFEED_OUTBOUND_PROXY") or "" if raw is None else raw).strip()
    if not value:
        return ""
    parts = urllib.parse.urlsplit(value)
    if parts.scheme not in {"http", "https", "socks5", "socks5h"}:
        return "OPENFEED_OUTBOUND_PROXY must start with http://, https://, socks5:// or socks5h://"
    if not parts.hostname:
        return "OPENFEED_OUTBOUND_PROXY has no host"
    return ""


def proxy_label() -> str:
    """The proxy without credentials, for the status page. Never shows a password."""
    value = proxy_url()
    if not value:
        return ""
    parts = urllib.parse.urlsplit(value)
    host = parts.hostname or ""
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{host}{port}"


def client(**kwargs: Any) -> httpx.Client:
    """An httpx.Client for a third-party or YouTube host, through the proxy when one is set."""
    proxy = proxy_url()
    if proxy:
        kwargs.setdefault("proxy", proxy)
    headers = dict(kwargs.pop("headers", None) or {})
    headers.setdefault("user-agent", USER_AGENT)
    kwargs["headers"] = headers
    return httpx.Client(**kwargs)


def get(url: str, **kwargs: Any) -> httpx.Response:
    """A one-off GET through `client()`."""
    timeout = kwargs.pop("timeout", 10.0)
    follow = kwargs.pop("follow_redirects", False)
    with client(timeout=timeout, follow_redirects=follow) as session:
        return session.get(url, **kwargs)


_MEDIA_CLIENT: httpx.Client | None = None
_MEDIA_CLIENT_PROXY = ""
_MEDIA_LOCK = threading.Lock()


def media_client() -> httpx.Client:
    """One pooled client for long-lived media streams from YouTube (the yt-dlp stream proxy).

    Pooled for the same reason as the Invidious proxy client: a fresh TLS handshake per
    range request is most of the seek delay. Rebuilt if the proxy setting changes.
    """
    global _MEDIA_CLIENT, _MEDIA_CLIENT_PROXY
    proxy = proxy_url()
    with _MEDIA_LOCK:
        if _MEDIA_CLIENT is None or _MEDIA_CLIENT_PROXY != proxy:
            options: dict[str, Any] = {
                "timeout": httpx.Timeout(60.0, connect=8.0),
                "follow_redirects": False,
                "limits": httpx.Limits(max_connections=32, max_keepalive_connections=16, keepalive_expiry=60.0),
            }
            if proxy:
                options["proxy"] = proxy
            _MEDIA_CLIENT = httpx.Client(**options)
            _MEDIA_CLIENT_PROXY = proxy
        return _MEDIA_CLIENT


def ytdlp_args() -> list[str]:
    """yt-dlp flags that route its requests through the outbound proxy."""
    proxy = proxy_url()
    return ["--proxy", proxy] if proxy else []
