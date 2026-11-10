"""Sign-in rate limits and lockouts, kept in Redis with short TTLs.

Two kinds of key, never a raw address:
- per account: `of:auth:<bucket>:a:<account id>`;
- per client: `of:auth:<bucket>:ip:<HMAC of the client address>`. The HMAC is keyed with
  the server secret, so the key cannot be reversed into an address by reading Redis, and
  it expires with the window.

When Redis cannot be reached the counters fall back to this process's memory, so a Redis
outage neither opens the door to unlimited guessing nor locks everyone out.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

from .config import get_settings

logger = logging.getLogger("openfeed.auth")


@dataclass(frozen=True)
class Rule:
    bucket: str
    limit: int
    window: int  # seconds


# Failed sign-ins (password or second step) per account, and per client address. Past the
# limit the attempt is refused with the same generic message until the window passes.
SIGNIN_ACCOUNT = Rule("signin", 8, 15 * 60)
SIGNIN_CLIENT = Rule("signin", 25, 15 * 60)
SECOND_STEP_ACCOUNT = Rule("2fa", 6, 15 * 60)
SETUP_CLIENT = Rule("setup", 8, 15 * 60)
SIGNUP_CLIENT = Rule("signup", 4, 60 * 60)
# A ceiling for the whole install, so open sign-up cannot be flooded from many addresses.
SIGNUP_GLOBAL = Rule("signup-all", 40, 60 * 60)
REAUTH_ACCOUNT = Rule("reauth", 8, 15 * 60)

_memory: dict[str, tuple[int, float]] = {}
_memory_lock = threading.Lock()
_client: Any = None
_client_lock = threading.Lock()


def _redis() -> Any:
    global _client
    with _client_lock:
        if _client is None:
            import redis

            _client = redis.Redis.from_url(get_settings().redis_url, socket_timeout=1.5, socket_connect_timeout=1.5)
        return _client


def reset_client() -> None:
    """For tests and after a settings change."""
    global _client
    with _client_lock:
        _client = None
    with _memory_lock:
        _memory.clear()


def _secret() -> bytes:
    settings = get_settings()
    return (settings.session_secret or settings.secret_key or "openfeed-unkeyed").encode("utf-8")


def client_hash(ip: str) -> str:
    """A keyed hash of the client address. The address itself is never stored anywhere."""
    return hmac.new(_secret(), b"client:" + str(ip or "unknown").encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def _trusted_networks() -> list[Any]:
    networks = []
    for part in get_settings().trusted_proxies.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            networks.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            logger.warning("OPENFEED_TRUSTED_PROXIES entry is not an address or network: %s", part)
    return networks


def _is_trusted(address: str, networks: list[Any]) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(ip in network for network in networks)


def client_ip(request: Any) -> str:
    """The connecting address, or the first untrusted hop of X-Forwarded-For when the
    connection comes from a proxy listed in OPENFEED_TRUSTED_PROXIES. Only ever hashed."""
    client = getattr(request, "client", None)
    peer = str(getattr(client, "host", "") or "")
    networks = _trusted_networks()
    if not networks or not _is_trusted(peer, networks):
        return peer or "unknown"
    headers = getattr(request, "headers", {}) or {}
    forwarded = [hop.strip() for hop in str(headers.get("x-forwarded-for") or "").split(",") if hop.strip()]
    for hop in reversed(forwarded):
        if not _is_trusted(hop, networks):
            return hop
    return forwarded[0] if forwarded else peer or "unknown"


def account_key(rule: Rule, account_id: Any) -> str:
    return f"of:auth:{rule.bucket}:a:{account_id}"


def client_key(rule: Rule, request: Any) -> str:
    return f"of:auth:{rule.bucket}:ip:{client_hash(client_ip(request))}"


def global_key(rule: Rule) -> str:
    return f"of:auth:{rule.bucket}:all"


def _memory_count(key: str, add: int, window: int) -> int:
    now = time.time()
    with _memory_lock:
        if len(_memory) > 20000:
            for stale in [k for k, (_, exp) in _memory.items() if exp <= now]:
                _memory.pop(stale, None)
        count, expires = _memory.get(key, (0, 0.0))
        if expires <= now:
            count, expires = 0, now + window
        count += add
        if add:
            _memory[key] = (count, expires)
        return count


def count(key: str, window: int) -> int:
    try:
        value = _redis().get(key)
        return int(value or 0)
    except Exception:  # noqa: BLE001 - fall back to this process
        return _memory_count(key, 0, window)


def hit(key: str, window: int) -> int:
    """Add one to the counter, starting its window on the first hit. Returns the new count."""
    try:
        client = _redis()
        pipe = client.pipeline()
        pipe.incr(key)
        pipe.expire(key, window, nx=True)
        value, _ = pipe.execute()
        return int(value or 0)
    except Exception:  # noqa: BLE001 - fall back to this process
        return _memory_count(key, 1, window)


def clear(key: str) -> None:
    try:
        _redis().delete(key)
    except Exception:  # noqa: BLE001
        pass
    with _memory_lock:
        _memory.pop(key, None)


def blocked(key: str, rule: Rule) -> bool:
    return count(key, rule.window) >= rule.limit


def remember_once(key: str, ttl: int) -> bool:
    """True the first time a key is seen within ttl, False after. For one-time exchanges."""
    try:
        return bool(_redis().set(key, "1", ex=max(1, int(ttl)), nx=True))
    except Exception:  # noqa: BLE001
        return _memory_count(key, 1, max(1, int(ttl))) == 1


def put(key: str, value: str, ttl: int | None = None) -> bool:
    try:
        return bool(_redis().set(key, value, ex=ttl, nx=True))
    except Exception:  # noqa: BLE001
        return False


def get(key: str) -> str | None:
    try:
        value = _redis().get(key)
    except Exception:  # noqa: BLE001
        return None
    return value.decode("utf-8") if isinstance(value, bytes) else value
