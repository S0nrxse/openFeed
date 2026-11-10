"""An in-memory stand-in for accounts.py, so sign-in flows run end to end without Postgres.

It mirrors the rules the SQL enforces (test_accounts_store checks the SQL itself): sessions
expire and die with a disabled account, a TOTP step is accepted once and only if later
than the last, recovery codes and links work once, the last admin cannot be demoted.
"""

from __future__ import annotations

import contextlib
import time
import uuid
from typing import Any, Iterator
from unittest.mock import patch

from openfeed import accounts, profiles


class FakeStore:
    def __init__(self) -> None:
        self.accounts: dict[str, dict[str, Any]] = {}
        self.identities: dict[str, dict[str, Any]] = {}
        self.sessions: dict[str, dict[str, Any]] = {}
        self.codes: dict[str, dict[str, Any]] = {}
        self.links: dict[str, dict[str, Any]] = {}
        self.settings: dict[str, Any] = {}
        self.deleted: list[str] = []
        self.now = time.time

    # accounts
    def _public(self, row: dict[str, Any] | None) -> dict[str, Any] | None:
        if not row:
            return None
        keys = ("id", "user_key", "username", "display_name", "role", "disabled", "totp_enabled_at", "created_at", "updated_at", "last_active_at")
        return {k: row.get(k) for k in keys}

    def get_account(self, account_id: str) -> dict[str, Any] | None:
        return self._public(self.accounts.get(str(account_id)))

    def get_by_username(self, username: str) -> dict[str, Any] | None:
        name = str(username or "").strip().lower()
        return self._public(next((a for a in self.accounts.values() if a["username"] == name), None))

    def get_by_user_key(self, user_key: str) -> dict[str, Any] | None:
        return self._public(next((a for a in self.accounts.values() if a["user_key"] == user_key), None))

    def list_accounts(self) -> list[dict[str, Any]]:
        out = []
        for a in self.accounts.values():
            row = self._public(a) or {}
            row["methods"] = sorted({i["method"] for i in self.identities.values() if i["account_id"] == a["id"]})
            row["sessions"] = sum(1 for s in self.sessions.values() if s["account_id"] == a["id"])
            out.append(row)
        return out

    def count_active_admins(self, exclude_id: str | None = None) -> int:
        return sum(1 for a in self.accounts.values() if a["role"] == "admin" and not a["disabled"] and a["id"] != exclude_id)

    def admin_exists(self) -> bool:
        return self.count_active_admins() > 0

    def create_account(self, username: str, display_name: str = "", *, role: str = "member", password_hash: str | None = None,
                       user_key: str | None = None, identity: tuple[str, str, str] | None = None, first_admin: bool = False) -> dict[str, Any]:
        name = accounts.clean_username(username)
        if first_admin and self.admin_exists():
            raise accounts.AccountError("setup_done", "Setup is already complete.", 409)
        if any(a["username"] == name for a in self.accounts.values()):
            raise accounts.AccountError("username_taken", "That username is taken. Pick another.", 409)
        aid = str(uuid.uuid4())
        self.accounts[aid] = {
            "id": aid, "user_key": user_key or accounts.new_user_key(), "username": name,
            "display_name": accounts.clean_display_name(display_name, name), "role": role, "disabled": False,
            "totp_enabled_at": None, "totp_secret": None, "totp_pending_secret": None, "totp_last_step": None,
            "created_at": None, "updated_at": None, "last_active_at": None,
        }
        if password_hash:
            self.set_password(aid, password_hash)
        if identity:
            self.add_identity(aid, identity[0], identity[1], identity[2])
        return self.get_account(aid) or {}

    def unique_username(self, base: str) -> str:
        stem = "".join(ch for ch in str(base or "").lower() if ch.isalnum()) or "user"
        candidate, n = stem, 1
        while any(a["username"] == candidate for a in self.accounts.values()):
            n += 1
            candidate = f"{stem}{n}"
        return candidate

    def update_account(self, account_id: str, *, display_name: str | None = None, role: str | None = None, disabled: bool | None = None) -> dict[str, Any] | None:
        row = self.accounts.get(str(account_id))
        if not row:
            return None
        if (role == "member" or disabled) and row["role"] == "admin" and not row["disabled"] and self.count_active_admins(row["id"]) == 0:
            raise accounts.AccountError("last_admin", "This is the only admin.", 409)
        if display_name is not None:
            row["display_name"] = display_name
        if role is not None:
            row["role"] = role
        if disabled is not None:
            row["disabled"] = bool(disabled)
            if disabled:
                self.delete_sessions(row["id"])
        return self.get_account(row["id"])

    def owner_state(self, user_key: str) -> str:
        row = next((a for a in self.accounts.values() if a["user_key"] == user_key or user_key.startswith(a["user_key"] + ":p:")), None)
        if not row:
            return "unknown"
        return "disabled" if row["disabled"] else "active"

    # identities
    def list_identities(self, account_id: str) -> list[dict[str, Any]]:
        return [dict(i) for i in self.identities.values() if i["account_id"] == str(account_id)]

    def find_identity(self, method: str, subject: str) -> dict[str, Any] | None:
        found = next((i for i in self.identities.values() if i["method"] == method and i["subject"] == str(subject)), None)
        if not found:
            return None
        return {**(self.get_account(found["account_id"]) or {}), "identity_id": found["id"], "method": method, "subject": subject}

    def add_identity(self, account_id: str, method: str, subject: str, label: str = "", data: dict[str, Any] | None = None) -> dict[str, Any]:
        if any(i["method"] == method and i["subject"] == str(subject) for i in self.identities.values()):
            raise accounts.AccountError("identity_taken", "That sign-in is already linked to an account.", 409)
        iid = str(uuid.uuid4())
        self.identities[iid] = {"id": iid, "account_id": str(account_id), "method": method, "subject": str(subject),
                                "label": label, "data": dict(data or {}), "created_at": None, "last_used_at": None}
        return dict(self.identities[iid])

    def remove_identity(self, account_id: str, identity_id: str) -> bool:
        if len(self.list_identities(account_id)) <= 1:
            raise accounts.AccountError("last_method", "This is your only way to sign in.", 409)
        row = self.identities.get(str(identity_id))
        if not row or row["account_id"] != str(account_id):
            return False
        del self.identities[str(identity_id)]
        return True

    def touch_identity(self, identity_id: str) -> None:
        return None

    def password_hash(self, account_id: str) -> str | None:
        row = next((i for i in self.identities.values() if i["account_id"] == str(account_id) and i["method"] == "password"), None)
        return row["data"].get("hash") if row else None

    def set_password(self, account_id: str, new_hash: str) -> None:
        row = next((i for i in self.identities.values() if i["account_id"] == str(account_id) and i["method"] == "password"), None)
        if row:
            row["data"] = {"hash": new_hash}
        else:
            self.add_identity(account_id, "password", str(account_id), "Password", {"hash": new_hash})

    # 2FA
    def totp_state(self, account_id: str) -> dict[str, Any]:
        row = self.accounts[str(account_id)]
        return {k: row[k] for k in ("totp_secret", "totp_enabled_at", "totp_last_step", "totp_pending_secret")}

    def set_totp_pending(self, account_id: str, secret: str) -> None:
        self.accounts[str(account_id)]["totp_pending_secret"] = secret

    def enable_totp(self, account_id: str, secret: str, step: int, hashes: list[str]) -> bool:
        row = self.accounts[str(account_id)]
        if row["totp_pending_secret"] != secret:
            return False
        row.update(totp_secret=secret, totp_pending_secret=None, totp_enabled_at=time.time(), totp_last_step=step)
        self.replace_recovery_codes(account_id, hashes)
        return True

    def disable_totp(self, account_id: str) -> None:
        self.accounts[str(account_id)].update(totp_secret=None, totp_pending_secret=None, totp_enabled_at=None, totp_last_step=None)
        self.codes = {k: v for k, v in self.codes.items() if v["account_id"] != str(account_id)}

    def claim_totp_step(self, account_id: str, step: int) -> bool:
        row = self.accounts[str(account_id)]
        if not row["totp_secret"] or (row["totp_last_step"] is not None and row["totp_last_step"] >= step):
            return False
        row["totp_last_step"] = step
        return True

    def replace_recovery_codes(self, account_id: str, hashes: list[str]) -> None:
        self.codes = {k: v for k, v in self.codes.items() if v["account_id"] != str(account_id)}
        for h in hashes:
            self.codes[f"{account_id}:{h}"] = {"account_id": str(account_id), "used": False}

    def use_recovery_code(self, account_id: str, code_hash: str) -> bool:
        row = self.codes.get(f"{account_id}:{code_hash}")
        if not row or row["used"]:
            return False
        row["used"] = True
        return True

    def recovery_codes_left(self, account_id: str) -> int:
        return sum(1 for v in self.codes.values() if v["account_id"] == str(account_id) and not v["used"])

    # sessions
    def create_session(self, account_id: str, token_hash: str, *, device: str, methods: list[str], idp_groups: list[str] | None,
                       idle_seconds: int, max_seconds: int, reauth: bool = True, signed_in_at: int | None = None) -> dict[str, Any]:
        sid = str(uuid.uuid4())
        now = self.now()
        self.sessions[sid] = {
            "id": sid, "token_hash": token_hash, "account_id": str(account_id), "created_at": signed_in_at or now, "last_seen_at": now,
            "idle_expires_at": now + min(idle_seconds, max_seconds), "expires_at": now + max_seconds,
            "reauth_at": now if reauth else None, "device": device, "methods": list(methods), "idp_groups": list(idp_groups or []),
        }
        return {"id": sid, "account_id": str(account_id)}

    def session_by_hash(self, token_hash: str) -> dict[str, Any] | None:
        now = self.now()
        for s in self.sessions.values():
            if s["token_hash"] == token_hash and s["expires_at"] > now and s["idle_expires_at"] > now:
                account = self.accounts.get(s["account_id"])
                if not account or account["disabled"]:
                    return None
                return {**s, "user_key": account["user_key"], "username": account["username"], "display_name": account["display_name"],
                        "role": account["role"], "disabled": account["disabled"], "totp_enabled_at": account["totp_enabled_at"]}
        return None

    def touch_session(self, session_id: str, account_id: str, idle_seconds: int) -> None:
        s = self.sessions.get(str(session_id))
        if s:
            s["last_seen_at"] = self.now()

    def rotate_session(self, session_id: str, new_hash: str, *, reauth: bool = False, methods: list[str] | None = None) -> bool:
        s = self.sessions.get(str(session_id))
        if not s:
            return False
        s["token_hash"] = new_hash
        if reauth:
            s["reauth_at"] = self.now()
        return True

    def list_sessions(self, account_id: str) -> list[dict[str, Any]]:
        return [dict(s) for s in self.sessions.values() if s["account_id"] == str(account_id)]

    def delete_session(self, account_id: str, session_id: str) -> bool:
        s = self.sessions.get(str(session_id))
        if not s or s["account_id"] != str(account_id):
            return False
        del self.sessions[str(session_id)]
        return True

    def delete_session_by_hash(self, token_hash: str) -> None:
        self.sessions = {k: v for k, v in self.sessions.items() if v["token_hash"] != token_hash}

    def delete_sessions(self, account_id: str, except_session_id: str | None = None) -> int:
        before = len(self.sessions)
        self.sessions = {k: v for k, v in self.sessions.items() if v["account_id"] != str(account_id) or k == except_session_id}
        return before - len(self.sessions)

    # links
    def create_link(self, kind: str, token_hash: str, ttl_seconds: int, *, created_by: str | None = None, account_id: str | None = None,
                    role: str = "member", note: str = "") -> dict[str, Any]:
        lid = str(uuid.uuid4())
        self.links[lid] = {"id": lid, "kind": kind, "token_hash": token_hash, "account_id": account_id, "role": role, "note": note,
                           "expires_at": self.now() + ttl_seconds, "used": False, "created_at": None, "created_by_name": None}
        return dict(self.links[lid])

    def _live(self, kind: str, token_hash: str) -> dict[str, Any] | None:
        return next((l for l in self.links.values() if l["kind"] == kind and l["token_hash"] == token_hash
                     and not l["used"] and l["expires_at"] > self.now()), None)

    def find_link(self, kind: str, token_hash: str) -> dict[str, Any] | None:
        link = self._live(kind, token_hash)
        if not link:
            return None
        account = self.accounts.get(str(link.get("account_id"))) or {}
        return {**link, "username": account.get("username"), "display_name": account.get("display_name")}

    def list_links(self, kind: str = "invite") -> list[dict[str, Any]]:
        return [dict(l) for l in self.links.values() if l["kind"] == kind and not l["used"]]

    def revoke_link(self, link_id: str) -> bool:
        return self.links.pop(str(link_id), None) is not None

    def redeem_invite(self, token_hash: str, username: str, display_name: str, password_hash: str) -> dict[str, Any]:
        link = self._live("invite", token_hash)
        if not link:
            raise accounts.AccountError("invite_invalid", "This invite link has expired or was already used.", 410)
        account = self.create_account(username, display_name, role=link["role"], password_hash=password_hash)
        link["used"] = True
        return account

    def redeem_reset(self, token_hash: str, new_hash: str) -> dict[str, Any]:
        link = self._live("reset", token_hash)
        if not link:
            raise accounts.AccountError("reset_invalid", "This reset link has expired or was already used.", 410)
        link["used"] = True
        self.set_password(link["account_id"], new_hash)
        self.delete_sessions(link["account_id"])
        return self.get_account(link["account_id"]) or {}

    # settings
    def get_setting(self, key: str, default: Any = None) -> Any:
        return self.settings.get(key, default)

    def set_setting(self, key: str, value: Any) -> None:
        self.settings[key] = value

    def registration_mode(self) -> str:
        return self.settings.get("registration_mode", accounts.DEFAULT_REGISTRATION)

    def delete_account(self, account_id: str) -> dict[str, Any] | None:
        row = self.accounts.get(str(account_id))
        if not row:
            return None
        if row["role"] == "admin" and not row["disabled"] and self.count_active_admins(row["id"]) == 0:
            raise accounts.AccountError("last_admin", "This is the only admin.", 409)
        del self.accounts[row["id"]]
        self.identities = {k: v for k, v in self.identities.items() if v["account_id"] != row["id"]}
        self.delete_sessions(row["id"])
        self.deleted.append(row["user_key"])
        return {"id": row["id"], "username": row["username"], "user_keys": [row["user_key"]], "removed": {}, "downloads": []}

    def local_data_counts(self) -> dict[str, int]:
        return {"playback_progress": 0}

    def adopt_local_data(self, target: str) -> dict[str, int]:
        self.settings["local_data_adopted"] = {"user_key": target}
        return {}

    def backfill_oidc_accounts(self, method: str) -> int:
        return 0

    NAMES = (
        "get_account", "get_by_username", "get_by_user_key", "list_accounts", "count_active_admins", "admin_exists",
        "create_account", "unique_username", "update_account", "owner_state", "list_identities", "find_identity",
        "add_identity", "remove_identity", "touch_identity", "password_hash", "set_password", "totp_state",
        "set_totp_pending", "enable_totp", "disable_totp", "claim_totp_step", "replace_recovery_codes",
        "use_recovery_code", "recovery_codes_left", "create_session", "session_by_hash", "touch_session",
        "rotate_session", "list_sessions", "delete_session", "delete_session_by_hash", "delete_sessions",
        "create_link", "find_link", "list_links", "revoke_link", "redeem_invite", "redeem_reset", "get_setting",
        "set_setting", "registration_mode", "delete_account", "local_data_counts", "adopt_local_data",
        "backfill_oidc_accounts",
    )


@contextlib.contextmanager
def fake_accounts(store: FakeStore | None = None) -> Iterator[FakeStore]:
    store = store or FakeStore()
    with contextlib.ExitStack() as stack:
        for name in FakeStore.NAMES:
            stack.enter_context(patch.object(accounts, name, getattr(store, name)))
        # Profiles live in their own module; an account here has none.
        stack.enter_context(patch.object(profiles, "list_profiles", return_value=[]))
        yield store
