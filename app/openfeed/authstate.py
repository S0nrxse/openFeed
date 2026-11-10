"""Short-lived, single-use state for sign-in ceremonies, kept on the server.

Passkey challenges and OIDC flows (state, nonce, PKCE verifier) live here between the
step that starts them and the step that finishes them. Each entry:

- is stored under a hash of its key, never the key itself, so reading Redis gives nothing
  a browser could replay;
- expires on its own (a few minutes);
- can be taken exactly once: `take` reads and deletes in one Redis transaction, so two
  requests racing with the same challenge cannot both get it.

Redis is shared by every app process. When it cannot be reached the store falls back to
this process's memory, like the rate limits in ratelimit.py: a ceremony that started and
finishes in different processes then fails closed ("start again"), never open.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from typing import Any

from . import ratelimit

logger = logging.getLogger("openfeed.auth")

_memory: dict[str, tuple[str, float]] = {}
_lock = threading.Lock()
_MAX_MEMORY = 20000


def _key(kind: str, key: str) -> str:
    digest = hashlib.sha256(f"{kind}:{key}".encode("utf-8")).hexdigest()
    return f"of:auth:flow:{kind}:{digest}"


def put(kind: str, key: str, data: dict[str, Any], ttl: int) -> None:
    """Store data for ttl seconds under (kind, key). A second put replaces the first."""
    name = _key(kind, key)
    body = json.dumps(data, separators=(",", ":"), sort_keys=True)
    ttl = max(1, int(ttl))
    try:
        ratelimit._redis().set(name, body, ex=ttl)
        return
    except Exception:  # noqa: BLE001 - fall back to this process
        logger.debug("authstate: redis unavailable, keeping %s in memory", kind)
    now = time.time()
    with _lock:
        if len(_memory) > _MAX_MEMORY:
            for stale in [k for k, (_, exp) in _memory.items() if exp <= now]:
                _memory.pop(stale, None)
        _memory[name] = (body, now + ttl)


def take(kind: str, key: str) -> dict[str, Any] | None:
    """The data stored under (kind, key), removed in the same step. None when missing,
    expired or already taken."""
    if not key:
        return None
    name = _key(kind, key)
    raw: Any = None
    try:
        pipe = ratelimit._redis().pipeline(transaction=True)
        pipe.get(name)
        pipe.delete(name)
        raw, _ = pipe.execute()
    except Exception:  # noqa: BLE001 - fall back to this process
        raw = None
    if raw is None:
        with _lock:
            body, expires = _memory.pop(name, (None, 0.0))
        raw = body if body is not None and expires > time.time() else None
    if raw is None:
        return None
    try:
        data = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
    except (ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def reset() -> None:
    """For tests."""
    with _lock:
        _memory.clear()
