from __future__ import annotations

import socket
import unittest
from unittest.mock import patch

from openfeed import netguard


class _FakeResponse:
    def __init__(self, status_code: int, location: str = "") -> None:
        self.status_code = status_code
        self.headers = {"location": location} if location else {}
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FakeFetcher:
    def __init__(self, responses) -> None:
        self.responses = responses
        self.urls: list[str] = []

    def get(self, url: str, follow_redirects: bool = False, **kwargs) -> _FakeResponse:
        assert follow_redirects is False
        self.urls.append(url)
        return self.responses.pop(0)


def _resolve(mapping: dict[str, list[str]]):
    """Fake a per-hostname resolution table; unknown hosts fail lookups closed."""
    def fake_getaddrinfo(host, port):
        addresses = mapping.get(host)
        if not addresses:
            raise socket.gaierror(f"no such host: {host}")
        return [(socket.AF_INET, None, None, "", (address, 0)) for address in addresses]

    return fake_getaddrinfo


_PUBLIC = {
    "example.com": ["93.184.216.34"],
    "second.example.com": ["8.8.4.4"],
    "first.example.com": ["93.184.216.34"],
}


class AssertPublicUrlTests(unittest.TestCase):
    def test_scheme_must_be_http_or_https(self) -> None:
        for evil in ("ftp://example.com/x", "file:///etc/passwd", "//example.com/x", "example.com/x"):
            with self.assertRaises(netguard.UnsafeExternalUrl):
                netguard.assert_public_url(evil)

    def test_loopback_and_private_hosts_are_refused(self) -> None:
        with patch.object(socket, "getaddrinfo", _resolve({"tunnel.example.com": ["127.0.0.1", "8.8.8.8"]})):
            with self.assertRaises(netguard.UnsafeExternalUrl):
                netguard.assert_public_url("https://tunnel.example.com/feed")
        with patch.object(socket, "getaddrinfo", _resolve({"internal.example.com": ["10.0.0.5"]})):
            with self.assertRaises(netguard.UnsafeExternalUrl):
                netguard.assert_public_url("https://internal.example.com/feed")

    def test_ipv4_mapped_ipv6_is_matched_underneath(self) -> None:
        with patch.object(socket, "getaddrinfo", _resolve({"tunnel.example.com": ["::ffff:127.0.0.1"]})):
            with self.assertRaises(netguard.UnsafeExternalUrl):
                netguard.assert_public_url("https://tunnel.example.com/feed")

    def test_all_public_answers_pass(self) -> None:
        with patch.object(socket, "getaddrinfo", _resolve({"example.com": ["8.8.8.8", "1.1.1.1"]})):
            self.assertEqual(netguard.assert_public_url("https://example.com/feed"), "https://example.com/feed")

    def test_unresolvable_host_fails_closed(self) -> None:
        with patch.object(socket, "getaddrinfo", _resolve({})):
            with self.assertRaises(netguard.UnsafeExternalUrl):
                netguard.assert_public_url("https://missing.example.com/feed")


class SafeGetTests(unittest.TestCase):
    def setUp(self) -> None:
        # A deterministic DNS table whose names all resolve somewhere public; a
        # redirect to a literal internal address still refuses because the check
        # matches the hostname, not a single fetched IP.
        self._resolver = patch.object(socket, "getaddrinfo", _resolve(dict(_PUBLIC, **{"hop1.example.com": ["93.184.216.34"]})))
        self._resolver.start()
        self.addCleanup(self._resolver.stop)

    def test_direct_200_without_redirects(self) -> None:
        fetcher = _FakeFetcher([_FakeResponse(200)])
        response = netguard.safe_get(fetcher, "https://example.com/entry")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(fetcher.urls, ["https://example.com/entry"])

    def test_redirect_to_private_host_is_refused(self) -> None:
        fetcher = _FakeFetcher([_FakeResponse(302, "http://127.0.0.1:9000/admin")])
        with patch.object(socket, "getaddrinfo", _resolve(_PUBLIC)):
            with self.assertRaises(netguard.UnsafeExternalUrl):
                netguard.safe_get(fetcher, "https://example.com/entry")

    def test_redirect_chain_is_rechecked_each_hop(self) -> None:
        fetcher = _FakeFetcher([
            _FakeResponse(302, "https://second.example.com/a"),
            _FakeResponse(200),
        ])
        with patch.object(socket, "getaddrinfo", _resolve(_PUBLIC)):
            response = netguard.safe_get(fetcher, "http://first.example.com/feed")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(fetcher.urls, ["http://first.example.com/feed", "https://second.example.com/a"])

    def test_relative_redirect_location_is_resolved(self) -> None:
        fetcher = _FakeFetcher([
            _FakeResponse(302, "/next"),
            _FakeResponse(200),
        ])
        with patch.object(socket, "getaddrinfo", _resolve(_PUBLIC)):
            response = netguard.safe_get(fetcher, "https://example.com/feed")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(fetcher.urls[-1], "https://example.com/next")

    def test_too_many_redirects_give_up(self) -> None:
        fetcher = _FakeFetcher([_FakeResponse(302, f"https://hop{i + 1}.example.com/x") for i in range(4)])
        with patch.object(socket, "getaddrinfo", _resolve(_PUBLIC)):
            with self.assertRaises(netguard.UnsafeExternalUrl):
                netguard.safe_get(fetcher, "https://first.example.com/feed")


if __name__ == "__main__":
    unittest.main()
