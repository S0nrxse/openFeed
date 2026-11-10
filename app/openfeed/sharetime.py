"""Start times in watch links: `?t=` the way YouTube writes and reads it.

YouTube accepts plain seconds (`t=90`), seconds with a unit (`t=90s`) and unit groups
(`t=1m30s`, `t=1h2m3s`); people also paste clock times (`t=1:30`). Share links are
written as plain seconds, which every one of those readers understands.
"""

from __future__ import annotations

import re

# A day: the same ceiling the player routes put on their own `t`.
MAX_START_SECONDS = 86400

_UNITS = re.compile(r"(?:(\d{1,5})h)?(?:(\d{1,6})m)?(?:(\d{1,7})s?)?")
_CLOCK = re.compile(r"(?:(\d{1,3}):)?(\d{1,4}):(\d{1,2})")


def parse_start(value: object) -> int | None:
    """Seconds from a `t` value, or None when it is missing or not a time at all.

    None and 0 differ on purpose: `t=0` is an explicit "from the start", which beats a
    stored resume point, while garbage is ignored and the resume point stands.
    """
    text = str(value or "").strip().lower()
    if not text:
        return None
    clock = _CLOCK.fullmatch(text)
    if clock:
        hours, minutes, seconds = (int(part or 0) for part in clock.groups())
        if seconds >= 60 or (clock.group(1) is not None and minutes >= 60):
            return None
        total = hours * 3600 + minutes * 60 + seconds
    else:
        units = _UNITS.fullmatch(text)
        if not units or not any(units.groups()):
            return None
        hours, minutes, seconds = (int(part or 0) for part in units.groups())
        total = hours * 3600 + minutes * 60 + seconds
    return min(total, MAX_START_SECONDS)


def clock(seconds: int) -> str:
    """m:ss, or h:mm:ss from an hour up: how the Share dialog names a start time."""
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"
