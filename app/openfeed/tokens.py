"""API tokens, so the API is usable by something other than a browser.

Every endpoint was reachable only with a session cookie, which meant that on any
deployment with login enabled there was no way for a script, a cron job or another
service to talk to OpenFeed at all. A bearer token closes that gap.

The plaintext token is generated here, shown once, and never stored. What goes in the
database is a SHA-256 hash plus a short clear prefix — the prefix only exists so a lookup
can find the candidate row without hashing every token in the table on every request, and
the comparison itself is constant-time.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Any

# Recognisable at a glance in logs and config files, and greppable if one ever leaks.
TOKEN_PREFIX = "ofd"
_PREFIX_BYTES = 6
_SECRET_BYTES = 32


def generate() -> tuple[str, str, str]:
    """Return (plaintext, prefix, hash) for a brand new token."""
    prefix = secrets.token_hex(_PREFIX_BYTES)
    secret = secrets.token_urlsafe(_SECRET_BYTES)
    plaintext = f"{TOKEN_PREFIX}_{prefix}_{secret}"
    return plaintext, prefix, hash_token(plaintext)


def hash_token(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def split(plaintext: str) -> str:
    """The lookup prefix inside a presented token, or "" if it is not one of ours.

    Split with maxsplit=2 deliberately: `secrets.token_urlsafe` draws from an alphabet
    that includes `_`, so an unbounded split puts the tail in extra parts and rejects
    perfectly valid tokens about half the time.
    """
    parts = (plaintext or "").split("_", 2)
    if len(parts) != 3 or parts[0] != TOKEN_PREFIX or not parts[2]:
        return ""
    prefix = parts[1]
    return prefix if len(prefix) == _PREFIX_BYTES * 2 and all(c in "0123456789abcdef" for c in prefix) else ""


def matches(plaintext: str, stored_hash: str) -> bool:
    """Constant-time comparison, so a timing signal cannot be used to guess a token."""
    return hmac.compare_digest(hash_token(plaintext), str(stored_hash or ""))


def bearer_from_header(header: str | None) -> str:
    """Pull the credential out of an Authorization header.

    Also accepts a bare token, because plenty of clients send `Authorization: <token>`
    without the scheme and failing them silently is a poor experience.
    """
    value = (header or "").strip()
    if not value:
        return ""
    if value.lower().startswith("bearer "):
        return value[7:].strip()
    return value


def public_view(row: dict[str, Any]) -> dict[str, Any]:
    """A token row as the API reports it — never including anything replayable."""
    return {
        "id": str(row.get("id") or ""),
        "name": row.get("name") or "",
        "prefix": f"{TOKEN_PREFIX}_{row.get('token_prefix') or ''}",
        "read_only": bool(row.get("read_only")),
        "created_at": row.get("created_at"),
        "last_used_at": row.get("last_used_at"),
        "revoked": row.get("revoked_at") is not None,
    }
