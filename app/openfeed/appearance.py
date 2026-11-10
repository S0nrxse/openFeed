"""Appearance: Dark, Light or Use device theme, the way YouTube's account menu offers it.

The choice is stored per user (per profile, where profiles exist) in `users.profile`
under "appearance", so it follows the person to another browser. The browser keeps a
mirror in localStorage and a cookie, which is what the inline <head> script reads to set
`data-theme` on <html> before the first paint on pages that are not rendered for a known
user (the profile picker, the sign-in error page).

`data-theme` is always one of the three choices. The light token set in
assets/css/00-tokens.css applies under `[data-theme="light"]`, and under
`[data-theme="system"]` inside `@media (prefers-color-scheme: light)`, so device mode
needs no script to follow the OS. Anything that never got the attribute stays dark, the
product default.

The player documents stay dark in every theme, as YouTube's player does.
"""

from __future__ import annotations

import json
from typing import Any

from . import db

CHOICES = ("dark", "light", "system")
DEFAULT = "dark"
LABELS = {"dark": "Dark theme", "light": "Light theme", "system": "Use device theme"}
# The short form the account-menu row shows after "Appearance: ".
SHORT_LABELS = {"dark": "Dark", "light": "Light", "system": "Device theme"}
STORAGE_KEY = "openfeed-theme"
COOKIE_NAME = "openfeed_theme"
# Browser chrome colour per resolved theme. Must equal --bg in each token set.
THEME_COLORS = {"dark": "#0a0a0b", "light": "#ffffff"}


def normalize(value: Any) -> str:
    """One of CHOICES, or "" for anything else (including nothing chosen)."""
    text = str(value or "").strip().lower()
    return text if text in CHOICES else ""


def get_choice(user_key: str = "local") -> str:
    """The stored choice, or "" when this user never picked one. Never raises.

    A plain read with no upsert: a page view for a user with no row yet has nothing
    stored, and creating the row is left to whatever first writes.
    """
    try:
        with db.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT profile->>'appearance' AS appearance FROM users WHERE authentik_sub = %s",
                    (user_key,),
                )
                row = cur.fetchone() or {}
    except Exception:
        return ""
    return normalize(row.get("appearance") if isinstance(row, dict) else "")


def save_choice(choice: str, user_key: str = "local") -> str:
    """Store a choice. Changes this one key, so it cannot race the rest of the profile."""
    value = normalize(choice)
    if not value:
        raise ValueError("appearance must be one of " + ", ".join(CHOICES))
    with db.connect() as conn:
        with conn.cursor() as cur:
            user_id = db._ensure_local_user_id(cur, user_key)
            cur.execute(
                "UPDATE users SET profile = COALESCE(profile, '{}'::jsonb) || jsonb_build_object('appearance', %s::text), "
                "updated_at = now() WHERE id = %s",
                (value, user_id),
            )
        conn.commit()
    return value


def head_script(stored: str = "") -> str:
    """The inline <head> script that sets data-theme before first paint.

    Order: the server's stored choice for this user, then this browser's mirror
    (localStorage, then the cookie), then the default. It also points both theme-color
    metas at the chosen theme, so an explicit choice wins over the OS in the browser chrome.
    Kept tiny and dependency-free: it runs before the stylesheet has even been parsed.
    """
    server = normalize(stored)
    return (
        "<script>(function(){var d=document.documentElement,c=" + json.dumps(server) + ",l='';"
        f"try{{l=localStorage.getItem('{STORAGE_KEY}')||''}}catch(e){{}}"
        f"if(!l){{var m=document.cookie.match(/(?:^|; ){COOKIE_NAME}=(dark|light|system)/);if(m)l=m[1]}}"
        "c=c||l;if(c!=='light'&&c!=='system')c='dark';d.setAttribute('data-theme',c);"
        f"if(c!=='system')document.querySelectorAll('meta[name=theme-color]').forEach(function(n){{n.content=c==='light'?'{THEME_COLORS['light']}':'{THEME_COLORS['dark']}'}})"
        "})()</script>"
    )


def theme_color_metas() -> str:
    """One theme-color per OS scheme. The head script overrides both for an explicit choice."""
    return (
        f'<meta name="theme-color" content="{THEME_COLORS["dark"]}" media="(prefers-color-scheme: dark)">'
        f'<meta name="theme-color" content="{THEME_COLORS["light"]}" media="(prefers-color-scheme: light)">'
    )


def head(stored: str = "") -> str:
    """Everything a page needs in <head> for the theme. The metas come first: the script fills them."""
    return theme_color_metas() + head_script(stored)
