"""Password hashing and the password policy.

Argon2id through argon2-cffi, with its default parameters (RFC 9106's second recommended
set). When those defaults change in a later release, `needs_rehash` says so and the sign-in
that just proved the password stores a fresh hash, so old hashes upgrade themselves.

The policy is the one NIST SP 800-63B recommends: a minimum length, a check against very
common passwords, and no composition rules ("one digit, one symbol") and no expiry.
"""

from __future__ import annotations

import unicodedata

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

MIN_LENGTH = 10
# Argon2 pre-hashes its input, so length costs nothing; the cap only bounds request size.
MAX_LENGTH = 256

_hasher = PasswordHasher()

# Spent on a sign-in for an unknown username, so its timing matches a wrong password.
_DUMMY_HASH = _hasher.hash("openfeed-timing-equaliser")

# Very common passwords of at least MIN_LENGTH characters, from the public breach top
# lists. Short ones are already refused by the length rule. Compared case-insensitively.
COMMON_PASSWORDS = frozenset({
    "1234567890", "12345678910", "123456789a", "1234567890a", "0123456789", "0987654321",
    "1111111111", "0000000000", "1234512345", "1122334455", "1q2w3e4r5t", "1q2w3e4r5t6y",
    "q1w2e3r4t5", "qwertyuiop", "qwertyuiop1", "qwerty12345", "qwerty123456", "qwerty1234",
    "asdfghjkl1", "asdfghjkl;", "zxcvbnm123", "1qaz2wsx3edc", "zaq12wsxcde3", "qazwsxedc1",
    "qazwsxedcrfv", "password12", "password123", "password1234", "password12345", "passw0rd12",
    "p@ssw0rd123", "p@ssword123", "password!1", "password01", "passwordpassword", "mypassword",
    "mypassword1", "iloveyou12", "iloveyou123", "iloveyou1234", "princess12", "princess123",
    "sunshine12", "sunshine123", "football12", "football123", "baseball12", "baseball123",
    "basketball", "basketball1", "superman12", "superman123", "starwars12", "starwars123",
    "welcome123", "welcome1234", "welcome2024", "welcome2025", "welcome2026", "letmein123",
    "letmein1234", "trustno1234", "dragon1234", "monkey12345", "abc1234567", "abcdef1234",
    "abcd123456", "abcdefghij", "a123456789", "aa12345678", "qwerty1234!", "changeme12",
    "changeme123", "administrator", "administrator1", "admin12345", "admin123456", "admin@123",
    "adminadmin", "rootroot12", "openfeed123", "openfeed1234", "youtube123", "youtube1234",
    "invidious1", "invidious123", "computer12", "computer123", "internet12", "internet123",
    "whatever12", "whatever123", "freedom123", "pokemon123", "jordan2323", "michael123",
    "charlie123", "liverpool1", "chelsea123", "arsenal123", "manchester", "blink18218",
    "metallica1", "slipknot666", "1q2w3e4r5t6y7u", "987654321a", "9876543210", "5555555555",
    "7777777777", "9999999999", "1212121212", "1231231231", "1234554321", "1029384756",
    "qwertyqwerty", "asdasdasd1", "asdfasdfasdf", "zxcvbnmasdf", "loveyou123", "iloveu1234",
    "summer2024", "summer2025", "summer2026", "winter2024", "winter2025", "winter2026",
    "spring2025", "spring2026", "autumn2025", "autumn2026", "january2025", "january2026",
    "december2025", "password2024", "password2025", "password2026", "qwerty2025", "qwerty2026",
})


class PasswordPolicyError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def normalize(password: str) -> str:
    """NFKC, so the same password typed on two keyboards hashes the same."""
    return unicodedata.normalize("NFKC", str(password or ""))


def check_policy(password: str, *, username: str = "", display_name: str = "") -> str:
    """Return the normalised password, or raise PasswordPolicyError with a sentence to show."""
    value = normalize(password)
    if len(value) < MIN_LENGTH:
        raise PasswordPolicyError("password_too_short", f"Use at least {MIN_LENGTH} characters. A few unrelated words make a good one.")
    if len(value) > MAX_LENGTH:
        raise PasswordPolicyError("password_too_long", f"Use at most {MAX_LENGTH} characters.")
    folded = value.casefold()
    if folded in COMMON_PASSWORDS or len(set(folded)) <= 2:
        raise PasswordPolicyError("password_common", "That password is one of the most common ones. Pick something less guessable.")
    for name in (username, display_name):
        name_folded = normalize(name).casefold().strip()
        if name_folded and (folded == name_folded or folded.startswith(name_folded) and len(folded) - len(name_folded) < 4):
            raise PasswordPolicyError("password_like_name", "Your password should not be your name or username.")
    return value


def hash_password(password: str) -> str:
    return _hasher.hash(normalize(password))


def verify(stored_hash: str | None, password: str) -> bool:
    """Constant work whether or not there is a hash, so a missing account cannot be timed."""
    candidate = normalize(password)[:MAX_LENGTH * 4]
    if not stored_hash:
        try:
            _hasher.verify(_DUMMY_HASH, candidate + "\0")
        except (VerificationError, InvalidHashError):
            pass
        return False
    try:
        return bool(_hasher.verify(stored_hash, candidate))
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(stored_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(stored_hash)
    except (InvalidHashError, ValueError):
        return True
