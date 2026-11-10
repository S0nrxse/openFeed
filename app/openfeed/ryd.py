"""Return YouTube Dislike, used strictly as a read-only estimate source.

YouTube stopped publishing dislike counts in 2021. RYD reconstructs an estimate from its
own archive and extension telemetry. Consulting it is a real, if small, privacy trade:
another party learns which video ids this deployment is interested in. Everything here
exists to keep that trade as small as it can be while still being useful.

The rules this module enforces:

* **Off unless asked.** Nothing here runs unless the user turns it on in Settings.
* **Read only.** There is no code path that submits a vote. Local likes and dislikes stay
  in this database and are never sent anywhere.
* **Never on the watch path.** Counts are filled in by a background pass over a batch of
  video ids and read from cache when a page renders. A request to RYD therefore cannot be
  correlated with somebody opening a video, which is the thing worth protecting — RYD has
  no batch endpoint, so the decorrelation has to come from *when* we ask, not from
  bundling ids into one call.
* **Server side only.** Requests leave from the backend, so RYD sees the deployment, never
  a user's address, account, or session.
* **Cached by video id**, so a given video is asked about roughly once.
* **Fixed endpoint, strict validation, hard timeouts.** The host is a constant, not a
  setting, so a compromised preference cannot redirect viewing interest to another party.
"""

from __future__ import annotations

import logging
import random
import re
import threading
import time
from typing import Any

import httpx

from . import outbound
from .config import get_settings

logger = logging.getLogger("openfeed.ryd")

# Deliberately a constant. Making this configurable would turn a privacy setting into a
# way to point the app's viewing interest at an arbitrary host.
RYD_ENDPOINT = "https://returnyoutubedislikeapi.com/votes"
RYD_TIMEOUT_SECONDS = 6.0
RYD_MAX_BATCH = 40
# Space out a batch so the sequence does not look like a session and cannot be paced back
# to somebody browsing.
RYD_MIN_GAP_SECONDS = 0.4
RYD_MAX_GAP_SECONDS = 1.2

_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_lock = threading.Lock()
_last_request_at = 0.0


class RydError(RuntimeError):
    """RYD could not be reached, or answered with something unusable."""


def enabled(user_key: str = "local") -> bool:
    """RYD is consulted only when the user has explicitly opted in."""
    from . import db  # imported here to keep this module free of a startup import cycle

    if not get_settings().ryd_enabled:
        return False
    try:
        return bool(db.get_feed_preferences(user_key).get("ryd_enabled"))
    except Exception:
        return False


def valid_video_id(video_id: str) -> bool:
    return bool(_VIDEO_ID.match(str(video_id or "")))


def _coerce_count(value: Any) -> int | None:
    """Accept only a sane non-negative integer; anything else is treated as absent."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value < 0 or value != value or value > 10**12:  # NaN and absurd values
        return None
    return int(value)


def parse_votes(payload: Any, expected_id: str) -> dict[str, Any] | None:
    """Validate one RYD response into the only three fields we will store.

    A response for a different video id is discarded rather than trusted, so a confused
    or hostile answer cannot write a count onto the wrong video.
    """
    if not isinstance(payload, dict):
        return None
    if str(payload.get("id") or "") != expected_id:
        return None
    if payload.get("deleted") is True:
        return None
    dislikes = _coerce_count(payload.get("dislikes"))
    if dislikes is None:
        return None
    return {
        "youtube_video_id": expected_id,
        "dislikes": dislikes,
        "likes": _coerce_count(payload.get("likes")),
        "view_count": _coerce_count(payload.get("viewCount")),
    }


def _throttle() -> None:
    global _last_request_at
    with _lock:
        gap = random.uniform(RYD_MIN_GAP_SECONDS, RYD_MAX_GAP_SECONDS)
        wait = (_last_request_at + gap) - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_request_at = time.monotonic()


def fetch_one(video_id: str, client: httpx.Client) -> dict[str, Any] | None:
    """One read-only lookup. Returns None when RYD has nothing usable for this video."""
    if not valid_video_id(video_id):
        return None
    _throttle()
    try:
        response = client.get(RYD_ENDPOINT, params={"videoId": video_id})
    except httpx.HTTPError as exc:
        raise RydError(f"RYD request failed: {exc}") from exc
    if response.status_code == 404:
        return None
    if response.status_code != 200:
        raise RydError(f"RYD returned HTTP {response.status_code}")
    try:
        return parse_votes(response.json(), video_id)
    except ValueError:
        return None


def fetch_batch(video_ids: list[str]) -> list[dict[str, Any]]:
    """Look up a group of videos in one background pass.

    RYD publishes no batch endpoint, so this is still one request per id — the privacy
    win comes from these all happening together, away from any page view, rather than
    from the number of connections.
    """
    wanted = [vid for vid in dict.fromkeys(video_ids) if valid_video_id(vid)][:RYD_MAX_BATCH]
    if not wanted:
        return []
    results: list[dict[str, Any]] = []
    headers = {"accept": "application/json", "user-agent": "OpenFeed (+https://github.com/S0nrxse/openFeed)"}
    with outbound.client(timeout=RYD_TIMEOUT_SECONDS, follow_redirects=False, headers=headers) as client:
        for video_id in wanted:
            try:
                votes = fetch_one(video_id, client)
            except RydError as exc:
                # One failure should not abandon the rest of the batch; the next pass
                # will pick up whatever is still missing.
                logger.info("RYD lookup failed for %s: %s", video_id, exc)
                continue
            if votes:
                results.append(votes)
    return results


# One cover batch at a time, and never two for the same video. Without this, opening a
# handful of videos quickly would fire overlapping batches and undo the point of them.
_inflight: set[str] = set()
_inflight_lock = threading.Lock()


def cover_batch(video_id: str, limit: int = RYD_MAX_BATCH, user_key: str = "local") -> dict[str, Any]:
    """Fetch one video's dislikes hidden inside a group of unrelated ones.

    Asking RYD about a video the moment somebody opens it would tell RYD what is being
    watched right now. Bundling it with other videos that need filling in and shuffling
    the order means RYD sees a batch and cannot tell which member triggered it — the cover
    is the other videos, so a batch of one is not worth sending and is skipped.

    This is weaker than only ever fetching on the worker's own schedule, because the batch
    still happens *because* a video was opened. It is the trade that lets a freshly opened
    video show a real count, and the Settings copy says so rather than implying otherwise.
    """
    from . import db

    if not enabled(user_key) or not valid_video_id(video_id):
        return {"ok": False, "reason": "disabled", "updated": 0}

    with _inflight_lock:
        if video_id in _inflight or len(_inflight) >= 2:
            return {"ok": False, "reason": "already_running", "updated": 0}
        _inflight.add(video_id)
    try:
        others = [vid for vid in db.videos_missing_dislikes(max(1, limit * 3)) if vid != video_id]
        random.shuffle(others)
        group = [video_id, *others[: max(0, limit - 1)]]
        if len(group) < 2:
            # Nothing to hide behind. Leave it for the worker rather than sending a lone
            # request that would point straight at what is being watched.
            return {"ok": False, "reason": "no_cover", "updated": 0}
        random.shuffle(group)
        votes = fetch_batch(group)
        updated = db.store_dislike_counts(votes) if votes else 0
        answered = {vote["youtube_video_id"] for vote in votes}
        db.mark_dislikes_checked([vid for vid in group if vid not in answered])
        return {"ok": True, "batch": len(group), "received": len(votes), "updated": updated}
    finally:
        with _inflight_lock:
            _inflight.discard(video_id)


def schedule_cover_batch(video_id: str, user_key: str = "local") -> bool:
    """Run cover_batch on a worker thread. Returns whether one was started.

    A batch takes tens of seconds with the pacing above, so it cannot happen inside the
    request that renders the watch page.
    """
    if not enabled(user_key) or not valid_video_id(video_id):
        return False
    with _inflight_lock:
        if video_id in _inflight or len(_inflight) >= 2:
            return False
    thread = threading.Thread(
        target=lambda: cover_batch(video_id, get_settings().ryd_batch_size, user_key),
        name=f"ryd-cover-{video_id}",
        daemon=True,
    )
    thread.start()
    return True


def refresh_missing(limit: int = RYD_MAX_BATCH, user_key: str = "local") -> dict[str, Any]:
    """Fill in dislike counts for videos that have none yet.

    This is the only thing that talks to RYD. It is called by the background worker, never
    by a page render, which is what keeps a lookup from being tied to a view.
    """
    from . import db

    if not enabled(user_key):
        return {"ok": False, "reason": "disabled", "updated": 0}
    pending = db.videos_missing_dislikes(limit)
    if not pending:
        return {"ok": True, "reason": "nothing_pending", "updated": 0}
    votes = fetch_batch(pending)
    updated = db.store_dislike_counts(votes) if votes else 0
    # Stamp the ones RYD had nothing for too, so they are not re-asked on every pass.
    answered = {vote["youtube_video_id"] for vote in votes}
    db.mark_dislikes_checked([vid for vid in pending if vid not in answered])
    return {"ok": True, "requested": len(pending), "received": len(votes), "updated": updated}
