"""Accounts, their sign-in methods, sessions, single-use links and install settings.

The storage layer under signin.py (sign-in flow and method registry) and account_pages.py
(the pages). Tables are in accounts.sql; docs/accounts.md describes the model.

- An account owns one `user_key`, the join key of every per-user table. Household profiles
  hang off it in account_profiles exactly as before accounts existed.
- An account can link several sign-in methods (account_identities): a password, Authentik
  or another OIDC provider, later passkeys. Each row says which method and which subject.
- Sessions live in auth_sessions. The cookie holds a random token; only its hash is here.
- Nothing here stores an IP address or an email address.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import unicodedata
from typing import Any

import psycopg

from . import db, profiles

logger = logging.getLogger("openfeed.auth")

ROLES = ("admin", "member")
REGISTRATION_MODES = ("closed", "invite", "open")
DEFAULT_REGISTRATION = "invite"
USERNAME_MAX = 32
DISPLAY_NAME_MAX = 60
_USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,31}$")
# Names that would read as the system speaking, or collide with the anonymous local user.
_RESERVED_USERNAMES = frozenset({"local", "admin-bot", "openfeed", "system", "root", "api-token", "anonymous", "setup"})
_SPACE_RE = re.compile(r"\s+")

ACCOUNT_COLUMNS = (
    "a.id, a.user_key, a.username, a.display_name, a.role, a.disabled, a.totp_enabled_at, "
    "a.created_at, a.updated_at, a.last_active_at"
)


class AccountError(ValueError):
    """A refused change. code is a stable string for the API, message the sentence shown."""

    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


# --- validation --------------------------------------------------------------------------


def clean_username(value: Any) -> str:
    name = unicodedata.normalize("NFKC", str(value or "")).strip().lower()
    if not _USERNAME_RE.match(name):
        raise AccountError(
            "invalid_username",
            "Usernames are 2 to 32 characters: letters, digits, dots, dashes and underscores, starting with a letter or digit.",
        )
    if name in _RESERVED_USERNAMES:
        raise AccountError("username_reserved", "That username is reserved. Pick another.")
    return name


def clean_display_name(value: Any, fallback: str = "") -> str:
    name = "".join(ch for ch in unicodedata.normalize("NFKC", str(value or "")) if unicodedata.category(ch)[0] != "C" or ch in "\t\n")
    name = _SPACE_RE.sub(" ", name).strip()
    if len(name) > DISPLAY_NAME_MAX:
        raise AccountError("display_name_too_long", f"Display names are at most {DISPLAY_NAME_MAX} characters.")
    return name or fallback


def new_user_key() -> str:
    """The user_key of an account created here. Opaque, random and never reused."""
    return f"u-{secrets.token_hex(10)}"


def _row(row: Any) -> dict[str, Any] | None:
    if not row:
        return None
    out = dict(row)
    for key in ("id", "account_id", "created_by"):
        if out.get(key) is not None:
            out[key] = str(out[key])
    return out


def _unique_violation(exc: Exception, index: str) -> bool:
    return isinstance(exc, psycopg.errors.UniqueViolation) and index in str(exc)


# --- accounts ----------------------------------------------------------------------------


def get_account(account_id: str) -> dict[str, Any] | None:
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT {ACCOUNT_COLUMNS} FROM accounts a WHERE a.id = %s", (str(account_id),))
        return _row(cur.fetchone())


def get_by_username(username: str) -> dict[str, Any] | None:
    name = unicodedata.normalize("NFKC", str(username or "")).strip().lower()
    if not name:
        return None
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT {ACCOUNT_COLUMNS} FROM accounts a WHERE lower(a.username) = %s", (name,))
        return _row(cur.fetchone())


def get_by_user_key(user_key: str) -> dict[str, Any] | None:
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT {ACCOUNT_COLUMNS} FROM accounts a WHERE a.user_key = %s", (user_key,))
        return _row(cur.fetchone())


def list_accounts() -> list[dict[str, Any]]:
    """Every account with its linked methods, for the admin list."""
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {ACCOUNT_COLUMNS},
                   COALESCE(array_agg(DISTINCT i.method) FILTER (WHERE i.method IS NOT NULL), '{{}}') AS methods,
                   (SELECT count(*) FROM auth_sessions s WHERE s.account_id = a.id AND s.expires_at > now()
                      AND s.idle_expires_at > now()) AS sessions
            FROM accounts a
            LEFT JOIN account_identities i ON i.account_id = a.id
            GROUP BY a.id
            ORDER BY (a.role = 'admin') DESC, lower(a.username)
            """
        )
        return [_row(row) for row in cur.fetchall()]  # type: ignore[misc]


def count_active_admins(exclude_id: str | None = None) -> int:
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM accounts WHERE role = 'admin' AND NOT disabled AND (%s::uuid IS NULL OR id <> %s::uuid)",
            (exclude_id, exclude_id),
        )
        return int((cur.fetchone() or {}).get("n") or 0)


def admin_exists() -> bool:
    return count_active_admins() > 0


def _insert_account(cur: Any, username: str, display_name: str, role: str, user_key: str) -> dict[str, Any]:
    try:
        cur.execute(
            f"""
            INSERT INTO accounts (user_key, username, display_name, role)
            VALUES (%s, %s, %s, %s)
            RETURNING {ACCOUNT_COLUMNS.replace("a.", "")}
            """,
            (user_key, username, display_name, role),
        )
    except psycopg.errors.UniqueViolation as exc:
        if "idx_accounts_username" in str(exc):
            raise AccountError("username_taken", "That username is taken. Pick another.", 409) from exc
        raise AccountError("account_exists", "An account for this sign-in already exists.", 409) from exc
    return _row(cur.fetchone()) or {}


def _set_password(cur: Any, account_id: str, password_hash: str) -> None:
    cur.execute(
        """
        INSERT INTO account_identities (account_id, method, subject, label, data)
        VALUES (%s, 'password', %s, 'Password', jsonb_build_object('hash', %s::text))
        ON CONFLICT (method, subject) DO UPDATE SET data = EXCLUDED.data, created_at = now()
        """,
        (account_id, str(account_id), password_hash),
    )


def create_account(
    username: str, display_name: str = "", *, role: str = "member", password_hash: str | None = None,
    user_key: str | None = None, identity: tuple[str, str, str] | None = None, first_admin: bool = False,
) -> dict[str, Any]:
    """Create an account, optionally with a password and/or one linked identity.

    first_admin makes this the setup step: it is refused once any admin exists, under a lock,
    so two browsers racing through /setup cannot both become the first admin.
    """
    name = clean_username(username)
    if role not in ROLES:
        raise AccountError("invalid_role", "Unknown role.")
    shown = clean_display_name(display_name, name)
    with db.connect() as conn:
        with conn.cursor() as cur:
            if first_admin:
                cur.execute("SELECT pg_advisory_xact_lock(hashtext('openfeed-setup'))")
                cur.execute("SELECT 1 FROM accounts WHERE role = 'admin' LIMIT 1")
                if cur.fetchone():
                    raise AccountError("setup_done", "Setup is already complete. Sign in instead.", 409)
            account = _insert_account(cur, name, shown, role, user_key or new_user_key())
            if password_hash:
                _set_password(cur, account["id"], password_hash)
            if identity:
                method, subject, label = identity
                cur.execute(
                    "INSERT INTO account_identities (account_id, method, subject, label) VALUES (%s, %s, %s, %s)",
                    (account["id"], method, subject, label),
                )
        conn.commit()
    return account


def unique_username(base: str) -> str:
    """A free username derived from base (an IdP's preferred_username), for OIDC accounts."""
    stem = re.sub(r"[^a-z0-9._-]", "", unicodedata.normalize("NFKC", str(base or "")).lower())[:24].strip("._-")
    if len(stem) < 2 or stem in _RESERVED_USERNAMES:
        stem = "user"
    with db.connect() as conn, conn.cursor() as cur:
        for attempt in range(50):
            candidate = stem if attempt == 0 else f"{stem}{attempt + 1}"
            cur.execute("SELECT 1 FROM accounts WHERE lower(username) = %s", (candidate,))
            if not cur.fetchone():
                return candidate
    return f"{stem}-{secrets.token_hex(3)}"


def update_account(account_id: str, *, display_name: str | None = None, role: str | None = None, disabled: bool | None = None) -> dict[str, Any] | None:
    sets: list[str] = []
    values: list[Any] = []
    if display_name is not None:
        sets.append("display_name = %s")
        values.append(display_name)
    if role is not None:
        if role not in ROLES:
            raise AccountError("invalid_role", "Unknown role.")
        sets.append("role = %s")
        values.append(role)
    if disabled is not None:
        sets.append("disabled = %s")
        values.append(bool(disabled))
    if not sets:
        return get_account(account_id)
    with db.connect() as conn:
        with conn.cursor() as cur:
            # Demoting or disabling the last admin would leave nobody able to manage the install.
            if role == "member" or disabled:
                cur.execute("SELECT pg_advisory_xact_lock(hashtext('openfeed-admins'))")
                cur.execute(
                    "SELECT count(*) AS n FROM accounts WHERE role = 'admin' AND NOT disabled AND id <> %s",
                    (str(account_id),),
                )
                others = int((cur.fetchone() or {}).get("n") or 0)
                cur.execute("SELECT role, disabled FROM accounts WHERE id = %s", (str(account_id),))
                current = cur.fetchone()
                if current and current["role"] == "admin" and not current["disabled"] and others == 0:
                    raise AccountError("last_admin", "This is the only admin. Make someone else an admin first.", 409)
            cur.execute(
                f"UPDATE accounts SET {', '.join(sets)}, updated_at = now() WHERE id = %s RETURNING id",
                (*values, str(account_id)),
            )
            found = cur.fetchone()
            # A disabled account's sessions end now, not at their next expiry.
            if found and disabled:
                cur.execute("DELETE FROM auth_sessions WHERE account_id = %s", (str(account_id),))
        conn.commit()
    return get_account(account_id) if found else None


def owner_state(user_key: str) -> str:
    """'active', 'disabled' or 'unknown' for the account that owns user_key (or its profile)."""
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT a.disabled FROM accounts a
            WHERE a.user_key = %s
               OR a.user_key = (SELECT p.account_key FROM account_profiles p WHERE p.user_key = %s LIMIT 1)
            LIMIT 1
            """,
            (user_key, user_key),
        )
        row = cur.fetchone()
    if not row:
        return "unknown"
    return "disabled" if row["disabled"] else "active"


# --- identities --------------------------------------------------------------------------


def list_identities(account_id: str) -> list[dict[str, Any]]:
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, account_id, method, subject, label, created_at, last_used_at
            FROM account_identities WHERE account_id = %s ORDER BY created_at
            """,
            (str(account_id),),
        )
        return [_row(row) for row in cur.fetchall()]  # type: ignore[misc]


def find_identity(method: str, subject: str) -> dict[str, Any] | None:
    """The identity and its account's columns, or None."""
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT i.id AS identity_id, i.method, i.subject, i.data, {ACCOUNT_COLUMNS}
            FROM account_identities i JOIN accounts a ON a.id = i.account_id
            WHERE i.method = %s AND i.subject = %s
            """,
            (method, str(subject)),
        )
        return _row(cur.fetchone())


def add_identity(account_id: str, method: str, subject: str, label: str = "", data: dict[str, Any] | None = None) -> dict[str, Any]:
    with db.connect() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute(
                    """
                    INSERT INTO account_identities (account_id, method, subject, label, data)
                    VALUES (%s, %s, %s, %s, %s::jsonb)
                    RETURNING id, account_id, method, subject, label, created_at, last_used_at
                    """,
                    (str(account_id), method, str(subject), label, json.dumps(data or {})),
                )
            except psycopg.errors.UniqueViolation as exc:
                raise AccountError("identity_taken", "That sign-in is already linked to an account.", 409) from exc
            row = cur.fetchone()
        conn.commit()
    return _row(row) or {}


def remove_identity(account_id: str, identity_id: str) -> bool:
    """Unlink one method. Refused when it is the account's last way to sign in."""
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtext('openfeed-identities'), hashtext(%s))", (str(account_id),))
            cur.execute("SELECT count(*) AS n FROM account_identities WHERE account_id = %s", (str(account_id),))
            if int((cur.fetchone() or {}).get("n") or 0) <= 1:
                raise AccountError("last_method", "This is your only way to sign in. Add another before removing it.", 409)
            cur.execute("DELETE FROM account_identities WHERE id = %s AND account_id = %s", (str(identity_id), str(account_id)))
            removed = bool(cur.rowcount)
        conn.commit()
    return removed


def touch_identity(identity_id: str) -> None:
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE account_identities SET last_used_at = now() WHERE id = %s", (str(identity_id),))
        conn.commit()


def password_hash(account_id: str) -> str | None:
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT data->>'hash' AS hash FROM account_identities WHERE account_id = %s AND method = 'password'",
            (str(account_id),),
        )
        row = cur.fetchone()
    return str(row["hash"]) if row and row.get("hash") else None


def set_password(account_id: str, new_hash: str) -> None:
    with db.connect() as conn:
        with conn.cursor() as cur:
            _set_password(cur, str(account_id), new_hash)
        conn.commit()


# --- two-step verification ---------------------------------------------------------------


def totp_state(account_id: str) -> dict[str, Any]:
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT totp_secret, totp_enabled_at, totp_last_step, totp_pending_secret FROM accounts WHERE id = %s",
            (str(account_id),),
        )
        return dict(cur.fetchone() or {})


def set_totp_pending(account_id: str, secret: str) -> None:
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE accounts SET totp_pending_secret = %s, updated_at = now() WHERE id = %s", (secret, str(account_id)))
        conn.commit()


def enable_totp(account_id: str, secret: str, step: int, recovery_hashes: list[str]) -> bool:
    """Turn 2FA on with the pending secret the first code proved, and store recovery codes."""
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE accounts SET totp_secret = totp_pending_secret, totp_pending_secret = NULL,
                       totp_enabled_at = now(), totp_last_step = %s, updated_at = now()
                WHERE id = %s AND totp_pending_secret = %s
                """,
                (int(step), str(account_id), secret),
            )
            if not cur.rowcount:
                return False
            _replace_recovery_codes(cur, str(account_id), recovery_hashes)
        conn.commit()
    return True


def disable_totp(account_id: str) -> None:
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE accounts SET totp_secret = NULL, totp_pending_secret = NULL, totp_enabled_at = NULL,
                       totp_last_step = NULL, updated_at = now()
                WHERE id = %s
                """,
                (str(account_id),),
            )
            cur.execute("DELETE FROM account_recovery_codes WHERE account_id = %s", (str(account_id),))
        conn.commit()


def claim_totp_step(account_id: str, step: int) -> bool:
    """Accept a code's time step only if it is later than the last accepted one.

    One UPDATE with the comparison in its WHERE clause, so two requests carrying the same
    code at the same moment cannot both win.
    """
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE accounts SET totp_last_step = %s
                WHERE id = %s AND totp_secret IS NOT NULL AND (totp_last_step IS NULL OR totp_last_step < %s)
                """,
                (int(step), str(account_id), int(step)),
            )
            claimed = bool(cur.rowcount)
        conn.commit()
    return claimed


def _replace_recovery_codes(cur: Any, account_id: str, hashes: list[str]) -> None:
    cur.execute("DELETE FROM account_recovery_codes WHERE account_id = %s", (account_id,))
    for code_hash in hashes:
        cur.execute("INSERT INTO account_recovery_codes (account_id, code_hash) VALUES (%s, %s)", (account_id, code_hash))


def replace_recovery_codes(account_id: str, hashes: list[str]) -> None:
    with db.connect() as conn:
        with conn.cursor() as cur:
            _replace_recovery_codes(cur, str(account_id), hashes)
        conn.commit()


def use_recovery_code(account_id: str, code_hash: str) -> bool:
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE account_recovery_codes SET used_at = now() WHERE account_id = %s AND code_hash = %s AND used_at IS NULL",
                (str(account_id), code_hash),
            )
            used = bool(cur.rowcount)
        conn.commit()
    return used


def recovery_codes_left(account_id: str) -> int:
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM account_recovery_codes WHERE account_id = %s AND used_at IS NULL", (str(account_id),))
        return int((cur.fetchone() or {}).get("n") or 0)


# --- sessions ----------------------------------------------------------------------------

SESSION_COLUMNS = "s.id, s.account_id, s.created_at, s.last_seen_at, s.idle_expires_at, s.expires_at, s.reauth_at, s.device, s.methods, s.idp_groups"


def create_session(
    account_id: str, token_hash: str, *, device: str, methods: list[str], idp_groups: list[str] | None,
    idle_seconds: int, max_seconds: int, reauth: bool = True, signed_in_at: int | None = None,
) -> dict[str, Any]:
    """signed_in_at (epoch) backdates created_at, for a session carried over from an older
    cookie: the IdP admin-group age limit then still counts from the real sign-in."""
    with db.connect() as conn:
        with conn.cursor() as cur:
            # Housekeeping: expired sessions are removed whenever anyone signs in.
            cur.execute("DELETE FROM auth_sessions WHERE expires_at < now() OR idle_expires_at < now()")
            cur.execute(
                """
                INSERT INTO auth_sessions (token_hash, account_id, created_at, idle_expires_at, expires_at, reauth_at, device, methods, idp_groups)
                VALUES (%s, %s, COALESCE(to_timestamp(%s), now()),
                        LEAST(now() + make_interval(secs => %s), now() + make_interval(secs => %s)),
                        now() + make_interval(secs => %s), CASE WHEN %s THEN now() END, %s, %s, %s::jsonb)
                RETURNING id, account_id, created_at, expires_at
                """,
                (token_hash, str(account_id), signed_in_at, int(idle_seconds), int(max_seconds), int(max_seconds), bool(reauth),
                 device[:80], list(methods), json.dumps(list(idp_groups or []))),
            )
            row = cur.fetchone()
            cur.execute("UPDATE accounts SET last_active_at = now() WHERE id = %s", (str(account_id),))
        conn.commit()
    return _row(row) or {}


def session_by_hash(token_hash: str) -> dict[str, Any] | None:
    """A live session joined with its account. Disabled accounts and expired sessions: None."""
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {SESSION_COLUMNS}, a.user_key, a.username, a.display_name, a.role, a.disabled, a.totp_enabled_at
            FROM auth_sessions s JOIN accounts a ON a.id = s.account_id
            WHERE s.token_hash = %s AND s.expires_at > now() AND s.idle_expires_at > now() AND NOT a.disabled
            """,
            (token_hash,),
        )
        return _row(cur.fetchone())


def touch_session(session_id: str, account_id: str, idle_seconds: int) -> None:
    """Slide the idle expiry. Written at most once a minute per session."""
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE auth_sessions SET last_seen_at = now(),
                       idle_expires_at = LEAST(expires_at, now() + make_interval(secs => %s))
                WHERE id = %s AND last_seen_at < now() - interval '60 seconds'
                """,
                (int(idle_seconds), str(session_id)),
            )
            if cur.rowcount:
                cur.execute(
                    "UPDATE accounts SET last_active_at = now() WHERE id = %s AND (last_active_at IS NULL OR last_active_at < now() - interval '5 minutes')",
                    (str(account_id),),
                )
        conn.commit()


def rotate_session(session_id: str, new_token_hash: str, *, reauth: bool = False, methods: list[str] | None = None) -> bool:
    """Give a session a new token (and optionally record a re-authentication now)."""
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE auth_sessions SET token_hash = %s,
                       reauth_at = CASE WHEN %s THEN now() ELSE reauth_at END,
                       methods = COALESCE(%s, methods)
                WHERE id = %s
                """,
                (new_token_hash, bool(reauth), methods, str(session_id)),
            )
            rotated = bool(cur.rowcount)
        conn.commit()
    return rotated


def list_sessions(account_id: str) -> list[dict[str, Any]]:
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {SESSION_COLUMNS} FROM auth_sessions s
            WHERE s.account_id = %s AND s.expires_at > now() AND s.idle_expires_at > now()
            ORDER BY s.last_seen_at DESC
            """,
            (str(account_id),),
        )
        return [_row(row) for row in cur.fetchall()]  # type: ignore[misc]


def delete_session(account_id: str, session_id: str) -> bool:
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM auth_sessions WHERE id = %s AND account_id = %s", (str(session_id), str(account_id)))
            removed = bool(cur.rowcount)
        conn.commit()
    return removed


def delete_session_by_hash(token_hash: str) -> None:
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM auth_sessions WHERE token_hash = %s", (token_hash,))
        conn.commit()


def delete_sessions(account_id: str, except_session_id: str | None = None) -> int:
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM auth_sessions WHERE account_id = %s AND (%s::uuid IS NULL OR id <> %s::uuid)",
                (str(account_id), except_session_id, except_session_id),
            )
            count = int(cur.rowcount or 0)
        conn.commit()
    return count


# --- single-use links --------------------------------------------------------------------


def create_link(
    kind: str, token_hash: str, ttl_seconds: int, *, created_by: str | None = None, account_id: str | None = None,
    role: str = "member", note: str = "",
) -> dict[str, Any]:
    with db.connect() as conn:
        with conn.cursor() as cur:
            if kind == "reset" and account_id:
                # One live reset link per account: a new one replaces the old.
                cur.execute("DELETE FROM account_links WHERE kind = 'reset' AND account_id = %s AND used_at IS NULL", (str(account_id),))
            cur.execute(
                """
                INSERT INTO account_links (kind, token_hash, account_id, role, note, created_by, expires_at)
                VALUES (%s, %s, %s, %s, %s, %s, now() + make_interval(secs => %s))
                RETURNING id, kind, account_id, role, note, created_by, created_at, expires_at, used_at
                """,
                (kind, token_hash, account_id, role, note[:80], created_by, int(ttl_seconds)),
            )
            row = cur.fetchone()
        conn.commit()
    return _row(row) or {}


def find_link(kind: str, token_hash: str) -> dict[str, Any] | None:
    """A link that is still usable: right kind, not used, not expired."""
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT l.id, l.kind, l.account_id, l.role, l.note, l.expires_at, a.username, a.display_name
            FROM account_links l LEFT JOIN accounts a ON a.id = l.account_id
            WHERE l.kind = %s AND l.token_hash = %s AND l.used_at IS NULL AND l.expires_at > now()
            """,
            (kind, token_hash),
        )
        return _row(cur.fetchone())


def _consume_link(cur: Any, kind: str, token_hash: str) -> dict[str, Any] | None:
    cur.execute(
        """
        UPDATE account_links SET used_at = now()
        WHERE kind = %s AND token_hash = %s AND used_at IS NULL AND expires_at > now()
        RETURNING id, kind, account_id, role
        """,
        (kind, token_hash),
    )
    return _row(cur.fetchone())


def list_links(kind: str = "invite") -> list[dict[str, Any]]:
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT l.id, l.kind, l.role, l.note, l.created_at, l.expires_at, l.used_at, c.username AS created_by_name
            FROM account_links l LEFT JOIN accounts c ON c.id = l.created_by
            WHERE l.kind = %s AND (l.used_at IS NULL AND l.expires_at > now())
            ORDER BY l.created_at DESC LIMIT 50
            """,
            (kind,),
        )
        return [_row(row) for row in cur.fetchall()]  # type: ignore[misc]


def revoke_link(link_id: str) -> bool:
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM account_links WHERE id = %s AND used_at IS NULL", (str(link_id),))
            removed = bool(cur.rowcount)
        conn.commit()
    return removed


def redeem_invite(token_hash: str, username: str, display_name: str, password_hash: str) -> dict[str, Any]:
    """Create the invited account and use up the invite, in one transaction."""
    name = clean_username(username)
    shown = clean_display_name(display_name, name)
    with db.connect() as conn:
        with conn.cursor() as cur:
            link = _consume_link(cur, "invite", token_hash)
            if not link:
                raise AccountError("invite_invalid", "This invite link has expired or was already used. Ask for a new one.", 410)
            account = _insert_account(cur, name, shown, link.get("role") or "member", new_user_key())
            _set_password(cur, account["id"], password_hash)
        conn.commit()
    return account


def redeem_reset(token_hash: str, new_password_hash: str) -> dict[str, Any]:
    """Set a new password from an admin's reset link and end every session of the account."""
    with db.connect() as conn:
        with conn.cursor() as cur:
            link = _consume_link(cur, "reset", token_hash)
            if not link or not link.get("account_id"):
                raise AccountError("reset_invalid", "This reset link has expired or was already used. Ask an admin for a new one.", 410)
            _set_password(cur, link["account_id"], new_password_hash)
            cur.execute("DELETE FROM auth_sessions WHERE account_id = %s", (link["account_id"],))
            cur.execute(f"SELECT {ACCOUNT_COLUMNS} FROM accounts a WHERE a.id = %s", (link["account_id"],))
            account = _row(cur.fetchone())
        conn.commit()
    return account or {}


# --- install settings --------------------------------------------------------------------


def get_setting(key: str, default: Any = None) -> Any:
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT value FROM app_settings WHERE key = %s", (key,))
        row = cur.fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: Any) -> None:
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app_settings (key, value) VALUES (%s, %s::jsonb)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
                """,
                (key, json.dumps(value)),
            )
        conn.commit()


def registration_mode() -> str:
    try:
        value = get_setting("registration_mode", DEFAULT_REGISTRATION)
    except Exception:  # noqa: BLE001 - closed is the safe reading of an unreadable setting
        return "closed"
    return value if value in REGISTRATION_MODES else DEFAULT_REGISTRATION


# --- deleting an account -----------------------------------------------------------------


def delete_account(account_id: str) -> dict[str, Any] | None:
    """Delete an account, its profiles and every row their user_keys own, in one transaction.

    Returns per-table counts, the user_keys removed and the download rows whose files the
    caller unlinks after the commit (main._after_user_keys_deleted). Refused for the last
    active admin.
    """
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtext('openfeed-admins'))")
            cur.execute("SELECT id, user_key, username, role, disabled FROM accounts WHERE id = %s", (str(account_id),))
            account = cur.fetchone()
            if not account:
                return None
            if account["role"] == "admin" and not account["disabled"]:
                cur.execute("SELECT count(*) AS n FROM accounts WHERE role = 'admin' AND NOT disabled AND id <> %s", (str(account_id),))
                if int((cur.fetchone() or {}).get("n") or 0) == 0:
                    raise AccountError("last_admin", "This is the only admin. Make someone else an admin first.", 409)
            main_key = str(account["user_key"])
            cur.execute("SELECT user_key FROM account_profiles WHERE account_key = %s", (main_key,))
            keys = [main_key] + [str(row["user_key"]) for row in cur.fetchall() if str(row["user_key"]) != main_key]
            totals: dict[str, int] = {}
            all_downloads: list[dict[str, Any]] = []
            for key in keys:
                counts, rows = profiles.delete_user_key_rows(cur, key)
                all_downloads.extend(rows)
                for table, n in counts.items():
                    totals[table] = totals.get(table, 0) + n
            cur.execute("DELETE FROM account_profiles WHERE account_key = %s", (main_key,))
            totals["account_profiles"] = int(cur.rowcount or 0)
            # Identities, sessions, recovery codes and reset links cascade.
            cur.execute("DELETE FROM accounts WHERE id = %s", (str(account_id),))
        conn.commit()
    return {"id": str(account_id), "username": account["username"], "user_keys": keys, "removed": totals, "downloads": all_downloads}


# --- keeping the data from before sign-in ------------------------------------------------

# Tokens minted while the server was open to anyone are not handed to the admin: anybody
# who could reach the server could have made one.
_ADOPT_SKIP = frozenset({"api_tokens"})


def local_data_counts() -> dict[str, int]:
    """How much the anonymous `local` user (and its profiles) has, for the setup offer."""
    counts: dict[str, int] = {}
    with db.connect() as conn, conn.cursor() as cur:
        for table in ("playback_progress", "user_video_feedback", "playlists", "downloads", "user_video_events"):
            cur.execute(f"SELECT count(*) AS n FROM {table} WHERE user_key = 'local' OR user_key LIKE 'local:p:%%'")
            counts[table] = int((cur.fetchone() or {}).get("n") or 0)
        cur.execute("SELECT count(*) AS n FROM account_profiles WHERE account_key = 'local' AND NOT is_main")
        counts["profiles"] = int((cur.fetchone() or {}).get("n") or 0)
    return counts


def _move_rows(conn: Any, cur: Any, table: str, old: str, new: str) -> tuple[int, int]:
    """Move one table's rows from user_key old to new. Returns (moved, dropped duplicates).

    Tries one UPDATE. If the new key already has a row a unique constraint covers (the same
    video's progress, say), it moves row by row instead and keeps the new key's row; the
    old duplicate is then dropped so nothing is left behind under the old key.
    """
    try:
        with conn.transaction():
            cur.execute(f"UPDATE {table} SET user_key = %s WHERE user_key = %s", (new, old))
            return int(cur.rowcount or 0), 0
    except psycopg.errors.UniqueViolation:
        pass
    moved = 0
    cur.execute(f"SELECT ctid FROM {table} WHERE user_key = %s", (old,))
    for row in cur.fetchall():
        try:
            with conn.transaction():
                cur.execute(f"UPDATE {table} SET user_key = %s WHERE ctid = %s", (new, row["ctid"]))
                moved += int(cur.rowcount or 0)
        except psycopg.errors.UniqueViolation:
            continue
    cur.execute(f"DELETE FROM {table} WHERE user_key = %s", (old,))
    return moved, int(cur.rowcount or 0)


def _move_user_key(conn: Any, cur: Any, old: str, new: str, counts: dict[str, int]) -> None:
    for table in profiles.USER_KEY_TABLES:
        if table in _ADOPT_SKIP:
            continue
        moved, dropped = _move_rows(conn, cur, table, old, new)
        counts[table] = counts.get(table, 0) + moved
        if dropped:
            counts[f"{table}_duplicates_dropped"] = counts.get(f"{table}_duplicates_dropped", 0) + dropped
    cur.execute(
        "UPDATE feed_impressions SET metadata = jsonb_set(metadata, '{user_key}', to_jsonb(%s::text)) WHERE metadata->>'user_key' = %s",
        (new, old),
    )
    counts["feed_impressions"] = counts.get("feed_impressions", 0) + int(cur.rowcount or 0)
    # The users row carries preferences (users.profile) and, by id, topic and channel
    # preferences and interactions. The old row is renamed so all of that follows; a row
    # the new key already has contributes its newer preference values and is replaced.
    cur.execute("SELECT id, profile FROM users WHERE authentik_sub = %s", (old,))
    old_row = cur.fetchone()
    if old_row:
        cur.execute("SELECT id, profile FROM users WHERE authentik_sub = %s", (new,))
        new_row = cur.fetchone()
        if new_row:
            cur.execute(
                "UPDATE users SET profile = COALESCE(profile, '{}'::jsonb) || %s::jsonb WHERE id = %s",
                (json.dumps(new_row.get("profile") or {}), old_row["id"]),
            )
            cur.execute("DELETE FROM users WHERE id = %s", (new_row["id"],))
        cur.execute("UPDATE users SET authentik_sub = %s, updated_at = now() WHERE id = %s", (new, old_row["id"]))
        counts["users"] = counts.get("users", 0) + 1


def adopt_local_data(target_user_key: str) -> dict[str, int]:
    """Move everything the `local` user and its profiles own to target_user_key.

    One transaction, serialised by a lock, and idempotent: a second run finds nothing left
    under `local` and moves nothing. Profiles move with their data; a profile's key is
    renamed from `local:p:x` to `<target>:p:x` so it reads as the new account's.
    """
    target = str(target_user_key or "").strip()
    if not target or target == "local" or target.startswith("local:"):
        raise AccountError("invalid_target", "Choose an account to keep the data.")
    counts: dict[str, int] = {}
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtext('openfeed-adopt-local'))")
            cur.execute("SELECT id, user_key, name, is_main FROM account_profiles WHERE account_key = 'local' ORDER BY position")
            local_profiles = [dict(row) for row in cur.fetchall()]
            _move_user_key(conn, cur, "local", target, counts)
            cur.execute("SELECT 1 FROM account_profiles WHERE account_key = %s AND is_main", (target,))
            target_has_main = bool(cur.fetchone())
            moved_profiles = 0
            for row in local_profiles:
                if row["is_main"]:
                    if target_has_main:
                        cur.execute("DELETE FROM account_profiles WHERE id = %s", (row["id"],))
                    else:
                        cur.execute(
                            "UPDATE account_profiles SET account_key = %s, user_key = %s, updated_at = now() WHERE id = %s",
                            (target, target, row["id"]),
                        )
                    continue
                old_key = str(row["user_key"])
                suffix = old_key[len("local"):] if old_key.startswith("local:p:") else f":p:{secrets.token_hex(8)}"
                new_key = f"{target}{suffix}"
                _move_user_key(conn, cur, old_key, new_key, counts)
                cur.execute(
                    "SELECT 1 FROM account_profiles WHERE account_key = %s AND lower(name) = lower(%s)", (target, row["name"])
                )
                name = row["name"] if not cur.fetchone() else f"{row['name'][:profiles.NAME_MAX - 9]} (before)"
                cur.execute(
                    "UPDATE account_profiles SET account_key = %s, user_key = %s, name = %s, updated_at = now() WHERE id = %s",
                    (target, new_key, name, row["id"]),
                )
                moved_profiles += 1
            counts["profiles"] = moved_profiles
            cur.execute(
                """
                INSERT INTO app_settings (key, value) VALUES ('local_data_adopted', %s::jsonb)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
                """,
                (json.dumps({"user_key": target}),),
            )
        conn.commit()
    return counts


# --- accounts for Authentik users from before accounts existed -------------------------


def backfill_oidc_accounts(method: str) -> int:
    """Give every Authentik user who signed in before accounts existed an account row.

    Their user_key stays the Authentik sub, so their data needs no migration. Users rows
    created for profiles or the local user are not people and are skipped (only rows the
    OIDC callback wrote carry source = authentik).
    """
    created = 0
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT u.authentik_sub, u.display_name, u.profile->>'username' AS username
            FROM users u
            WHERE u.profile->>'source' = 'authentik'
              AND NOT EXISTS (SELECT 1 FROM accounts a WHERE a.user_key = u.authentik_sub)
            """
        )
        rows = [dict(row) for row in cur.fetchall()]
    for row in rows:
        sub = str(row["authentik_sub"])
        try:
            create_account(
                unique_username(row.get("username") or row.get("display_name") or "user"),
                str(row.get("display_name") or ""), user_key=sub, identity=(method, sub, "Authentik"),
            )
            created += 1
        except AccountError as exc:
            logger.info("could not create an account for an existing Authentik user: %s", exc.code)
    return created


# --- phase 2 sign-in methods: passkeys, OIDC providers, trusted header -------------------


def list_method_identities(account_id: str, method: str) -> list[dict[str, Any]]:
    """One account's identities of one method, with their verifier data (passkey public
    keys and counters). Never holds a secret."""
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, account_id, method, subject, label, data, created_at, last_used_at
            FROM account_identities WHERE account_id = %s AND method = %s ORDER BY created_at
            """,
            (str(account_id), method),
        )
        return [_row(row) for row in cur.fetchall()]  # type: ignore[misc]


def update_identity_data(identity_id: str, data: dict[str, Any], *, used: bool = True) -> bool:
    """Merge data into an identity's verifier data (a passkey's new signature counter),
    and record its use."""
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE account_identities SET data = data || %s::jsonb,
                       last_used_at = CASE WHEN %s THEN now() ELSE last_used_at END
                WHERE id = %s
                """,
                (json.dumps(data), bool(used), str(identity_id)),
            )
            changed = bool(cur.rowcount)
        conn.commit()
    return changed


def rename_identity(account_id: str, identity_id: str, label: str) -> bool:
    """Rename one of this account's identities (a passkey). Another account's: False."""
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE account_identities SET label = %s WHERE id = %s AND account_id = %s",
                (label, str(identity_id), str(account_id)),
            )
            changed = bool(cur.rowcount)
        conn.commit()
    return changed


def create_account_with_invite(
    token_hash: str, username: str, display_name: str, *, identity: tuple[str, str, str], user_key: str | None = None,
) -> dict[str, Any]:
    """Create an account signed in by an outside identity (an OIDC provider) from an invite
    link, using up the invite in the same transaction."""
    name = clean_username(username)
    shown = clean_display_name(display_name, name)
    with db.connect() as conn:
        with conn.cursor() as cur:
            link = _consume_link(cur, "invite", token_hash)
            if not link:
                raise AccountError("invite_invalid", "This invite link has expired or was already used. Ask for a new one.", 410)
            account = _insert_account(cur, name, shown, link.get("role") or "member", user_key or new_user_key())
            method, subject, label = identity
            try:
                cur.execute(
                    "INSERT INTO account_identities (account_id, method, subject, label) VALUES (%s, %s, %s, %s)",
                    (account["id"], method, subject, label),
                )
            except psycopg.errors.UniqueViolation as exc:
                raise AccountError("identity_taken", "That sign-in is already linked to an account.", 409) from exc
        conn.commit()
    return account
