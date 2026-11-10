"""Two-step verification: TOTP codes from an authenticator app, and recovery codes.

- TOTP is RFC 6238 through pyotp: SHA-1, 6 digits, 30 seconds, which is what every
  authenticator app understands. One step of clock skew is accepted either way.
- A code works once. The time step of the last accepted code is stored on the account
  and a code must be from a later step (accounts.claim_totp_step does that atomically),
  so a code read over a shoulder or replayed from a log is refused.
- The QR code is drawn on this server as SVG by segno. The secret never leaves the
  server in a URL to anybody's QR service.
- Recovery codes are random, shown once, and stored only as keyed hashes. Each one
  works once.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import re
import secrets
import time

import pyotp
import segno

DIGITS = 6
INTERVAL = 30
# Steps either side of now that are still accepted: about 30 seconds of clock drift.
SKEW_STEPS = 1
RECOVERY_CODE_COUNT = 10
_RECOVERY_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # no 0/o, 1/l/i
_NON_DIGIT = re.compile(r"\D")
_NON_CODE = re.compile(r"[^a-z0-9]")


def new_secret() -> str:
    return pyotp.random_base32(32)


def provisioning_uri(secret: str, account_name: str, issuer: str = "OpenFeed") -> str:
    return pyotp.TOTP(secret, digits=DIGITS, interval=INTERVAL).provisioning_uri(name=account_name or "account", issuer_name=issuer)


def qr_svg(uri: str) -> str:
    """An inline SVG of the otpauth URI. Dark on light whatever the theme, so it scans."""
    qr = segno.make(uri, error="m", micro=False)
    out = io.BytesIO()
    qr.save(out, kind="svg", scale=4, border=3, dark="#0a0a0b", light="#ffffff", xmldecl=False, svgns=True,
            title="QR code for your authenticator app", svgclass="totp-qr-svg", lineclass=None, omitsize=True)
    return out.getvalue().decode("utf-8")


def format_secret(secret: str) -> str:
    """The secret in groups of four, for typing in by hand."""
    return " ".join(secret[i:i + 4] for i in range(0, len(secret), 4))


def clean_code(code: str) -> str:
    return _NON_DIGIT.sub("", str(code or ""))


def matching_step(secret: str, code: str, now: float | None = None) -> int | None:
    """The time step the code belongs to, within the allowed skew, or None.

    The caller still has to claim the step (accounts.claim_totp_step) before treating the
    code as accepted: that is what makes a code single-use.
    """
    digits = clean_code(code)
    if len(digits) != DIGITS or not secret:
        return None
    totp = pyotp.TOTP(secret, digits=DIGITS, interval=INTERVAL)
    moment = time.time() if now is None else now
    current = int(moment // INTERVAL)
    found = None
    # Check every candidate step, so the time taken does not say which one matched.
    for step in range(current - SKEW_STEPS, current + SKEW_STEPS + 1):
        if hmac.compare_digest(totp.generate_otp(step), digits) and found is None:
            found = step
    return found


def new_recovery_codes(count: int = RECOVERY_CODE_COUNT) -> list[str]:
    """Ten codes like `k7wq-3mzd-ph`: 10 characters, about 50 bits each."""
    codes = []
    for _ in range(count):
        raw = "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(10))
        codes.append(f"{raw[:4]}-{raw[4:8]}-{raw[8:]}")
    return codes


def normalize_recovery_code(code: str) -> str:
    return _NON_CODE.sub("", str(code or "").lower())


def hash_recovery_code(code: str, key: bytes) -> str:
    """Keyed, so a copy of the table alone cannot even be checked against guesses."""
    return hmac.new(key or b"openfeed", b"recovery:" + normalize_recovery_code(code).encode("ascii", "ignore"), hashlib.sha256).hexdigest()
