"""Household profiles inside one account ("Who's watching").

An account is the identity a request signs in as: the Authentik `sub`, or `local` when
sign-in is off. Before profiles existed that identity was also the `user_key` every
per-user table is keyed by. A profile is a second `user_key` owned by the same account,
so history, likes, playlists, preferences, impressions and the ranker caches separate
per profile without any change to those tables.

The main profile is the account itself: its `user_key` is the `account_key`, so creating
the first extra profile moves no data. Until somebody adds a profile there are no rows at
all and the account behaves exactly as it did before.

Subscriptions are per profile as well (`user_subscriptions`, keyed by user_key): each
profile is its own person with its own list. The feeds themselves are shared, so a
channel two profiles follow is still fetched once.

Selection is resolved in main.py from a signed, HttpOnly cookie that names a profile id,
and that id is only honoured when this module lists it under the requesting account.
"""

from __future__ import annotations

import re
import secrets
import uuid
from typing import Any

from . import catalog_scope, db

MAX_PROFILES = 6
NAME_MAX = 40
# Closed list. The hex values live in assets/css/36-profiles.css; DESIGN.md explains why a
# profile, alone in the product, gets a hue: it identifies a person, not a state.
COLORS = ("graphite", "slate", "sage", "clay", "plum", "steel")
DEFAULT_COLOR = "graphite"

# Every table whose rows belong to one user_key. Deleting a profile deletes from each of
# these. tests/test_profiles.py parses schema.sql and fails if a new user_key table is
# not listed here, so a later table cannot quietly survive its profile's deletion.
USER_KEY_TABLES = (
    "user_video_events",
    "user_video_feedback",
    "playback_progress",
    "playlists",  # playlist_items cascade
    "user_comment_feedback",
    "user_subscriptions",  # the feed rows stay; nobody left following one stops its polling
    "subscription_collections",  # subscription_collection_members cascade
    "channel_notifications",
    "downloads",
    "api_tokens",
    "takeout_channel_affinity",
    "takeout_watched_videos",
    "takeout_imports",
    "takeout_subscriptions",
    "post_poll_votes",
    "user_history_settings",
    "library_trash",
    "video_provenance",  # why this person may be shown a catalog video (catalog_scope.py)
    "catalog_claims",
)

_SPACE_RE = re.compile(r"\s+")


class ProfileError(ValueError):
    """A refused change. `code` is the API's detail string, `message` the sentence shown."""

    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def clean_name(value: Any) -> str:
    name = _SPACE_RE.sub(" ", str(value or "")).strip()
    if not name:
        raise ProfileError("name_required", "Give the profile a name.")
    if len(name) > NAME_MAX:
        raise ProfileError("name_too_long", f"Profile names are at most {NAME_MAX} characters.")
    return name


def clean_color(value: Any) -> str:
    color = str(value or DEFAULT_COLOR).strip().lower()
    if color not in COLORS:
        raise ProfileError("invalid_color", "Pick one of the listed colours.")
    return color


def new_user_key(account_key: str) -> str:
    """A user_key for a non-main profile: the account plus a random suffix.

    Prefixed by the account so a row read in psql says whose it is; the random part
    keeps a recreated profile with the same name from inheriting a deleted one's rows.
    """
    return f"{account_key}:p:{secrets.token_hex(8)}"


def implicit_main(account_key: str, name: str) -> dict[str, Any]:
    """The account's one profile before any row exists."""
    return {
        "id": "", "account_key": account_key, "user_key": account_key, "name": name or "Main",
        "color": DEFAULT_COLOR, "is_kids": False, "is_main": True, "position": 0,
    }


def _public(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out["id"] = str(out.get("id") or "")
    return out


def list_profiles(account_key: str) -> list[dict[str, Any]]:
    """The account's stored profiles, main first. Empty until profiles are managed."""
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, account_key, user_key, name, color, is_kids, is_main, position, created_at
                FROM account_profiles
                WHERE account_key = %s
                ORDER BY is_main DESC, position, created_at
                """,
                (account_key,),
            )
            return [_public(dict(row)) for row in cur.fetchall()]


def profile_for_user_key(user_key: str) -> dict[str, Any] | None:
    """The profile a user_key belongs to, for credentials (tokens) that carry only the key."""
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, account_key, user_key, name, color, is_kids, is_main FROM account_profiles WHERE user_key = %s",
                (user_key,),
            )
            row = cur.fetchone()
    return _public(dict(row)) if row else None


def _lock_account(cur: Any, account_key: str) -> None:
    # Serialises create/delete for one account so two tabs cannot both pass the limit check.
    cur.execute("SELECT pg_advisory_xact_lock(hashtext('openfeed-profiles'), hashtext(%s))", (account_key,))


def _ensure_main(cur: Any, account_key: str, main_name: str) -> None:
    cur.execute(
        """
        INSERT INTO account_profiles (account_key, user_key, name, is_main, position)
        VALUES (%s, %s, %s, true, 0)
        ON CONFLICT DO NOTHING
        """,
        (account_key, account_key, clean_name(str(main_name or "Main")[:NAME_MAX])),
    )


def _duplicate(exc: Exception) -> bool:
    return "idx_account_profiles_name" in str(exc)


def create_profile(account_key: str, name: Any, color: Any = DEFAULT_COLOR, is_kids: bool = False, main_name: str = "Main") -> dict[str, Any]:
    clean = clean_name(name)
    hue = clean_color(color)
    with db.connect() as conn:
        with conn.cursor() as cur:
            _lock_account(cur, account_key)
            _ensure_main(cur, account_key, main_name)
            cur.execute("SELECT count(*) AS n, COALESCE(max(position), 0) AS top FROM account_profiles WHERE account_key = %s", (account_key,))
            counts = cur.fetchone() or {}
            if int(counts.get("n") or 0) >= MAX_PROFILES:
                raise ProfileError("profile_limit", f"An account can have at most {MAX_PROFILES} profiles.", 409)
            cur.execute("SELECT 1 FROM account_profiles WHERE account_key = %s AND lower(name) = lower(%s)", (account_key, clean))
            if cur.fetchone():
                raise ProfileError("name_taken", "Another profile already has that name.", 409)
            # A second person on the install: from now on each one sees only their part of
            # the shared catalog, and the catalog so far stays with whoever built it.
            catalog_scope.mark_shared(cur, "profiles")
            cur.execute(
                """
                INSERT INTO account_profiles (account_key, user_key, name, color, is_kids, is_main, position)
                VALUES (%s, %s, %s, %s, %s, false, %s)
                RETURNING id, account_key, user_key, name, color, is_kids, is_main, position, created_at
                """,
                (account_key, new_user_key(account_key), clean, hue, bool(is_kids), int(counts.get("top") or 0) + 1),
            )
            row = cur.fetchone()
        conn.commit()
    return _public(dict(row))


def update_profile(
    account_key: str, profile_id: str, *, name: Any = None, color: Any = None, is_kids: bool | None = None, main_name: str = "Main",
) -> dict[str, Any] | None:
    """Rename, recolour or flip the kids flag. None when the id is not this account's."""
    if not _valid_uuid(profile_id) and profile_id != "main":
        return None
    sets: list[str] = []
    values: list[Any] = []
    if name is not None:
        sets.append("name = %s")
        values.append(clean_name(name))
    if color is not None:
        sets.append("color = %s")
        values.append(clean_color(color))
    with db.connect() as conn:
        with conn.cursor() as cur:
            _lock_account(cur, account_key)
            # "main" lets the implicit main profile be renamed before any row exists.
            if profile_id == "main":
                _ensure_main(cur, account_key, main_name)
                cur.execute("SELECT id, is_main FROM account_profiles WHERE account_key = %s AND is_main", (account_key,))
            else:
                cur.execute("SELECT id, is_main FROM account_profiles WHERE account_key = %s AND id = %s", (account_key, profile_id))
            target = cur.fetchone()
            if not target:
                return None
            if is_kids is not None:
                if is_kids and target["is_main"]:
                    raise ProfileError("main_not_kids", "The main profile manages the others, so it cannot be a kids profile.")
                sets.append("is_kids = %s")
                values.append(bool(is_kids))
            if sets:
                try:
                    cur.execute(
                        f"UPDATE account_profiles SET {', '.join(sets)}, updated_at = now() WHERE id = %s",
                        (*values, target["id"]),
                    )
                except Exception as exc:
                    if _duplicate(exc):
                        raise ProfileError("name_taken", "Another profile already has that name.", 409) from exc
                    raise
            cur.execute(
                "SELECT id, account_key, user_key, name, color, is_kids, is_main, position, created_at FROM account_profiles WHERE id = %s",
                (target["id"],),
            )
            row = cur.fetchone()
        conn.commit()
    return _public(dict(row)) if row else None


def delete_profile(account_key: str, profile_id: str) -> dict[str, Any] | None:
    """Delete a non-main profile and every row its user_key owns, in one transaction.

    Returns the deleted profile with per-table counts and the download rows whose files
    the caller should try to unlink (only after this commit, and only if nothing else
    references them). None when the id is not one of this account's profiles.
    """
    if not _valid_uuid(profile_id):
        return None
    with db.connect() as conn:
        with conn.cursor() as cur:
            _lock_account(cur, account_key)
            cur.execute(
                "SELECT id, user_key, name, is_main FROM account_profiles WHERE account_key = %s AND id = %s",
                (account_key, profile_id),
            )
            target = cur.fetchone()
            if not target:
                return None
            if target["is_main"]:
                raise ProfileError("main_profile", "The main profile is the account itself and cannot be deleted.", 409)
            key = str(target["user_key"])
            # Belt and braces: a non-main row always has a derived key, never the account's.
            if key == account_key:
                raise ProfileError("main_profile", "The main profile is the account itself and cannot be deleted.", 409)
            counts, downloads = delete_user_key_rows(cur, key)
            cur.execute("DELETE FROM account_profiles WHERE id = %s", (target["id"],))
        conn.commit()
    return {"id": str(target["id"]), "user_key": key, "name": target["name"], "removed": counts, "downloads": downloads}


def delete_user_key_rows(cur: Any, key: str) -> tuple[dict[str, int], list[dict[str, Any]]]:
    """Delete every row one user_key owns, inside the caller's transaction.

    Returns per-table counts and the download rows, whose files the caller unlinks after
    the commit. Shared by profile deletion and account deletion (accounts.delete_account).
    """
    counts: dict[str, int] = {}
    cur.execute("SELECT * FROM downloads WHERE user_key = %s", (key,))
    downloads = [dict(row) for row in cur.fetchall()]
    for table in USER_KEY_TABLES:
        cur.execute(f"DELETE FROM {table} WHERE user_key = %s", (key,))
        counts[table] = int(cur.rowcount or 0)
    cur.execute("DELETE FROM feed_impressions WHERE metadata->>'user_key' = %s", (key,))
    counts["feed_impressions"] = int(cur.rowcount or 0)
    # The users row holds preferences (users.profile JSON); interactions, topic and
    # channel preferences and any remaining impressions cascade from it.
    cur.execute("DELETE FROM users WHERE authentik_sub = %s", (key,))
    counts["users"] = int(cur.rowcount or 0)
    return counts, downloads


def _valid_uuid(value: Any) -> bool:
    try:
        uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return False
    return True
