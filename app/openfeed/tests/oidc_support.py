"""A fake OpenID Connect provider for unit tests: discovery, JWKS, token and userinfo
answers, served by patching httpx.get / httpx.post (oidc.py calls them).

It signs real RS256 ID tokens with a fresh key, so oidc.verify_id_token runs for real.
Tests change `claims`, `token_status`, `userinfo` or call `id_token(...)` themselves to
produce the failure they need.
"""

from __future__ import annotations

import time
import urllib.parse
from typing import Any
from unittest.mock import patch

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from openfeed import oidc


class _Answer:
    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> Any:
        return self.payload


class FakeIdP:
    def __init__(self, issuer: str = "https://auth.example/application/o/openfeed", client_id: str = "id", kid: str = "k1") -> None:
        self.issuer = issuer.rstrip("/")
        self.client_id = client_id
        self.kid = kid
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public = jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key(), as_dict=True)
        public.update({"kid": kid, "use": "sig", "alg": "RS256"})
        self.jwks = {"keys": [public]}
        base = urllib.parse.urlsplit(self.issuer)
        root = f"{base.scheme}://{base.netloc}"
        self.discovery = {
            "issuer": self.issuer + "/", "authorization_endpoint": root + "/authorize", "token_endpoint": root + "/token",
            "userinfo_endpoint": root + "/userinfo", "jwks_uri": root + "/jwks",
            "token_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic"],
        }
        self.claims: dict[str, Any] = {"sub": "ak-sub-123", "preferred_username": "alice", "name": "Alice", "groups": ["users"]}
        self.userinfo: dict[str, Any] | None = None  # None: same claims as the ID token
        self.token_status = 200
        self.nonce = ""
        self.override_token: str | None = None
        self.codes_used: list[str] = []
        self.token_requests: list[dict[str, Any]] = []

    def id_token(self, *, key: Any = None, alg: str = "RS256", kid: str | None = None, **changes: Any) -> str:
        """A signed ID token for the current nonce. A change of None drops that claim."""
        now = int(time.time())
        payload = {"iss": self.issuer + "/", "aud": self.client_id, "iat": now, "exp": now + 300, "nonce": self.nonce, **self.claims}
        payload.update(changes)
        payload = {k: v for k, v in payload.items() if v is not None}
        return jwt.encode(payload, key if key is not None else self.key, algorithm=alg, headers={"kid": kid or self.kid})

    # httpx replacements
    def get(self, url: str, *args: Any, **kwargs: Any) -> _Answer:
        if url.endswith("/.well-known/openid-configuration") or url.endswith("openid-configuration"):
            return _Answer(self.discovery)
        if url.endswith("/jwks"):
            return _Answer(self.jwks)
        if url.endswith("/userinfo"):
            info = dict(self.claims) if self.userinfo is None else self.userinfo
            return _Answer(info)
        return _Answer({}, 404)

    def post(self, url: str, *args: Any, data: dict[str, Any] | None = None, **kwargs: Any) -> _Answer:
        data = dict(data or {})
        self.token_requests.append(data)
        code = str(data.get("code") or "")
        if self.token_status >= 400:
            return _Answer({"error": "invalid_grant"}, self.token_status)
        # A real provider refuses a code twice.
        if code in self.codes_used:
            return _Answer({"error": "invalid_grant"}, 400)
        self.codes_used.append(code)
        token = self.override_token if self.override_token is not None else self.id_token()
        return _Answer({"access_token": "at", "id_token": token, "token_type": "Bearer"})

    def install(self, case: Any) -> "FakeIdP":
        """Patch httpx for the test case and clear oidc's caches."""
        oidc.clear_caches()
        case.addCleanup(oidc.clear_caches)
        case.enterContext(patch.object(oidc.httpx, "get", side_effect=self.get))
        case.enterContext(patch.object(oidc.httpx, "post", side_effect=self.post))
        return self

    def authorize(self, location: str) -> tuple[str, str]:
        """Read state and nonce from the redirect to the provider; remember the nonce."""
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(location).query)
        self.nonce = query["nonce"][0]
        return query["state"][0], self.nonce
