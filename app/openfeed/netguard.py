"""Host-publicity checks for URLs the app fetches server-side.

OpenFeed dereferences several categories of URLs that are not fully under operator
control: "public" Invidious instances configured in Settings, stream URLs returned
by Invidious/yt-dlp, and feed URLs subscribed by users. A hostile or misconfigured
value can otherwise turn the app container into an internal-network scanner, since
Postgres, Redis, Qdrant and Typesense all sit on the same bridge network.

The rule: a URL passed to one of these fetch paths must resolve to an address that
is not private, loopback, link-local, multicast, reserved or otherwise unroutable,
even if that changes between DNS answers or across redirects.
"""

from __future__ import annotations

import ipaddress
import socket
import urllib.parse
from typing import Any

import httpx


class UnsafeExternalUrl(Exception):
    """Raised when a URL would send a server-side fetch at an internal address."""


def _address_internal(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def assert_public_url(url: str, *, allow_private: bool = False) -> str:
    """Return the URL (scheme-normalized) if every resolved address is routable.

    Raises UnsafeExternalUrl otherwise. Set allow_private=True only for fetches the
    operator explicitly advertises as internal (none of the current callers do).
    """
    parsed = urllib.parse.urlsplit(str(url or ""))
    if parsed.scheme not in {"http", "https"}:
        raise UnsafeExternalUrl(f"unsupported URL scheme: {parsed.scheme!r}")
    host = parsed.hostname
    if not host:
        raise UnsafeExternalUrl("URL has no host")
    if allow_private:
        return str(url)
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise UnsafeExternalUrl(f"cannot resolve host {host!r}") from exc
    for info in infos:
        address = info[4][0]
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        if _address_internal(ip):
            raise UnsafeExternalUrl(f"refusing to fetch internal address {address} ({host})")
    return str(url)


def is_public_url(url: str) -> bool:
    try:
        assert_public_url(url)
    except UnsafeExternalUrl:
        return False
    return True


_MAX_REDIRECT_HOPS = 5


def safe_get(fetcher: Any, url: str, **kwargs: Any) -> httpx.Response:
    """GET with redirects followed only through hosts that resolve to public addresses.

    Plain follow_redirects=True would let a 302 from a nominally-public host move the
    request onto the container network. passed `fetcher` is httpx-style (module-level
    httpx.get, a Client, or a bounded client context object).
    """
    current_url = str(url)
    response: httpx.Response | None = None
    for _ in range(_MAX_REDIRECT_HOPS + 1):
        assert_public_url(current_url)
        response = fetcher.get(current_url, follow_redirects=False, **kwargs)
        if not (300 <= response.status_code < 400):
            return response
        location = response.headers.get("location")
        if not location:
            return response
        response.close()
        current_url = urllib.parse.urljoin(current_url, location)
    raise UnsafeExternalUrl("too many redirects while looking for a safe public host")
