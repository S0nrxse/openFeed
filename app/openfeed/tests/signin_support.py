"""Test helpers for the phase 2 sign-in methods (test_signin_methods.py).

- SigninStore: accounts_support.FakeStore plus the storage calls passkeys, OIDC
  providers and the trusted header add (verifier data, rename, invite with an identity).
- SoftAuthenticator: a software WebAuthn authenticator (ES256, attestation "none") that
  answers navigator.credentials.create/get options the way a browser would, so
  passkeys.py and py_webauthn verify real signatures in the tests.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import uuid
from typing import Any, Iterator
from unittest.mock import patch

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

from openfeed import accounts
from openfeed.tests.accounts_support import FakeStore


class SigninStore(FakeStore):
    EXTRA = ("list_method_identities", "update_identity_data", "rename_identity", "create_account_with_invite")

    def find_identity(self, method: str, subject: str) -> dict[str, Any] | None:
        found = next((i for i in self.identities.values() if i["method"] == method and i["subject"] == str(subject)), None)
        if not found:
            return None
        return {**(self.get_account(found["account_id"]) or {}), "identity_id": found["id"], "method": method,
                "subject": subject, "data": dict(found.get("data") or {})}

    def list_method_identities(self, account_id: str, method: str) -> list[dict[str, Any]]:
        return [dict(i, data=dict(i.get("data") or {})) for i in self.identities.values()
                if i["account_id"] == str(account_id) and i["method"] == method]

    def update_identity_data(self, identity_id: str, data: dict[str, Any], *, used: bool = True) -> bool:
        row = self.identities.get(str(identity_id))
        if not row:
            return False
        row["data"] = {**(row.get("data") or {}), **data}
        if used:
            row["last_used_at"] = self.now()
        return True

    def rename_identity(self, account_id: str, identity_id: str, label: str) -> bool:
        row = self.identities.get(str(identity_id))
        if not row or row["account_id"] != str(account_id):
            return False
        row["label"] = label
        return True

    def create_account_with_invite(self, token_hash: str, username: str, display_name: str, *, identity: tuple[str, str, str],
                                   user_key: str | None = None) -> dict[str, Any]:
        link = self._live("invite", token_hash)
        if not link:
            raise accounts.AccountError("invite_invalid", "This invite link has expired or was already used.", 410)
        account = self.create_account(username, display_name, role=link["role"], user_key=user_key, identity=identity)
        link["used"] = True
        return account


@contextlib.contextmanager
def signin_accounts(store: SigninStore) -> Iterator[SigninStore]:
    """Patch accounts.py with store, including the phase 2 calls."""
    from openfeed.tests.accounts_support import fake_accounts

    with fake_accounts(store), contextlib.ExitStack() as stack:
        for name in SigninStore.EXTRA:
            stack.enter_context(patch.object(accounts, name, getattr(store, name)))
        yield store


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def unb64url(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class SoftAuthenticator:
    """One passkey on a software authenticator."""

    UP, UV, BE, BS, AT = 0x01, 0x04, 0x08, 0x10, 0x40

    def __init__(self, rp_id: str, origin: str, *, synced: bool = False) -> None:
        self.rp_id = rp_id
        self.origin = origin
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = os.urandom(16)
        self.sign_count = 0
        self.synced = synced
        self.user_handle = b""

    @property
    def id(self) -> str:
        return b64url(self.credential_id)

    def _cose(self) -> bytes:
        numbers = self.key.public_key().public_numbers()
        return cbor2.dumps({1: 2, 3: -7, -1: 1, -2: numbers.x.to_bytes(32, "big"), -3: numbers.y.to_bytes(32, "big")})

    def _auth_data(self, flags: int, attested: bool = False) -> bytes:
        data = hashlib.sha256(self.rp_id.encode("utf-8")).digest() + bytes([flags]) + self.sign_count.to_bytes(4, "big")
        if attested:
            data += uuid.UUID(int=0).bytes + len(self.credential_id).to_bytes(2, "big") + self.credential_id + self._cose()
        return data

    def _client_data(self, kind: str, challenge: str, origin: str | None) -> bytes:
        return json.dumps({"type": kind, "challenge": challenge, "origin": origin or self.origin, "crossOrigin": False}).encode("utf-8")

    def create(self, public_key: dict[str, Any], *, uv: bool = True, origin: str | None = None) -> dict[str, Any]:
        """navigator.credentials.create() for these options (as JSON from the server)."""
        excluded = {c["id"] for c in public_key.get("excludeCredentials") or []}
        if self.id in excluded:
            raise ValueError("InvalidStateError: already registered")
        self.user_handle = unb64url(public_key["user"]["id"])
        flags = self.UP | (self.UV if uv else 0) | self.AT | ((self.BE | self.BS) if self.synced else 0)
        attestation = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": self._auth_data(flags, attested=True)})
        return {
            "id": self.id, "rawId": self.id, "type": "public-key", "clientExtensionResults": {},
            "response": {"clientDataJSON": b64url(self._client_data("webauthn.create", public_key["challenge"], origin)),
                         "attestationObject": b64url(attestation), "transports": ["internal", "hybrid"]},
        }

    def get(self, public_key: dict[str, Any], *, uv: bool = True, origin: str | None = None, counter_step: int = 1,
            user_handle: bytes | None = None, challenge: str | None = None) -> dict[str, Any]:
        """navigator.credentials.get() for these options."""
        allowed = [c["id"] for c in public_key.get("allowCredentials") or []]
        if allowed and self.id not in allowed:
            raise ValueError("NotAllowedError: this authenticator holds none of the allowed credentials")
        if not self.synced:
            self.sign_count += counter_step
        flags = self.UP | (self.UV if uv else 0) | ((self.BE | self.BS) if self.synced else 0)
        auth_data = self._auth_data(flags)
        client_data = self._client_data("webauthn.get", challenge or public_key["challenge"], origin)
        signature = self.key.sign(auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256()))
        handle = self.user_handle if user_handle is None else user_handle
        response = {"clientDataJSON": b64url(client_data), "authenticatorData": b64url(auth_data), "signature": b64url(signature)}
        if handle:
            response["userHandle"] = b64url(handle)
        return {"id": self.id, "rawId": self.id, "type": "public-key", "clientExtensionResults": {}, "response": response}
