"""Live chat and chat replay, fetched by the server through yt-dlp. Read-only.

Invidious has no chat API. yt-dlp does: it reads YouTube's chat the way the YouTube page
does (the `live_chat` "subtitle" of a stream), as a maintained extractor rather than a
scraper this project would have to keep alive. The browser never talks to YouTube. It
polls OpenFeed, and OpenFeed runs yt-dlp.

* **Replay** (a finished stream or premiere): one yt-dlp run per video downloads the whole
  replay and keeps it in memory. Messages carry their offset into the video, so the page
  shows them in step with the player. The file is read while it is still being written,
  so the first minutes are available long before the download finishes.
* **Live** (a stream or premiere happening now): one yt-dlp process per video follows the
  chat for as long as someone is polling it. It stops 45 seconds after the last poll, after
  four hours regardless, and with the server process.

What is sent: the video's watch URL, from the server, with no cookies and no account. Chat
text and author names come back. Author photos, custom emoji images and stickers are not
fetched, so no Google image host is ever contacted on the page's behalf.

Feature switch: `live_chat_enabled` in the user's playback preferences, off by default.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from collections import OrderedDict, deque
from typing import Any, Callable

from . import ytdlp

logger = logging.getLogger("openfeed.livechat")

REPLAY_TIMEOUT = 600
REPLAY_RETRY_AFTER = 120.0
REPLAY_CACHE_SIZE = 4
MAX_REPLAY_MESSAGES = 40000
WINDOW_LIMIT = 600

LIVE_IDLE_SECONDS = 45.0
LIVE_MAX_SECONDS = 4 * 3600.0
LIVE_MAX_SESSIONS = 3
LIVE_BUFFER = 400
LIVE_BATCH = 200

MAX_TEXT = 600


# --- Parsing -----------------------------------------------------------------------------

def _text(value: Any) -> str:
    """Text from YouTube's three shapes: simpleText, runs, and view-model content."""
    if not isinstance(value, dict):
        return str(value or "") if isinstance(value, str) else ""
    if "simpleText" in value:
        return str(value.get("simpleText") or "")
    if "content" in value:
        return str(value.get("content") or "")
    parts = []
    for run in value.get("runs") or []:
        if not isinstance(run, dict):
            continue
        if "text" in run:
            parts.append(str(run.get("text") or ""))
        elif isinstance(run.get("emoji"), dict):
            emoji = run["emoji"]
            # A standard emoji's id is the character itself. A channel's custom emoji is
            # an image on Google's servers, so it becomes its :shortcut: instead.
            if emoji.get("isCustomEmoji"):
                shortcuts = emoji.get("shortcuts") or []
                parts.append(str(shortcuts[0]) if shortcuts else "")
            else:
                parts.append(str(emoji.get("emojiId") or ""))
    return "".join(parts)


def _role(renderer: dict[str, Any]) -> str:
    roles = []
    for badge in renderer.get("authorBadges") or []:
        inner = (badge or {}).get("liveChatAuthorBadgeRenderer") or {}
        icon = str(((inner.get("icon") or {}).get("iconType")) or "").upper()
        if icon == "OWNER":
            roles.append("owner")
        elif icon == "MODERATOR":
            roles.append("moderator")
        elif icon == "VERIFIED":
            roles.append("verified")
        elif inner.get("customThumbnail"):
            roles.append("member")
    for role in ("owner", "moderator", "member", "verified"):
        if role in roles:
            return role
    return ""


def _message(kind: str, renderer: dict[str, Any], offset: int | None) -> dict[str, Any] | None:
    if not isinstance(renderer, dict):
        return None
    author = _text(renderer.get("authorName")).strip()
    amount = ""
    if kind == "liveChatTextMessageRenderer":
        category, text = "text", _text(renderer.get("message"))
    elif kind == "liveChatPaidMessageRenderer":
        category, text = "paid", _text(renderer.get("message"))
        amount = _text(renderer.get("purchaseAmountText"))
    elif kind == "liveChatPaidStickerRenderer":
        category, text = "paid", "Sent a sticker"
        amount = _text(renderer.get("purchaseAmountText"))
    elif kind == "liveChatMembershipItemRenderer":
        category = "member"
        headline = _text(renderer.get("headerPrimaryText")) or _text(renderer.get("headerSubtext"))
        body = _text(renderer.get("message"))
        text = f"{headline}: {body}" if headline and body else headline or body
    elif kind == "liveChatSponsorshipsGiftPurchaseAnnouncementRenderer":
        header = ((renderer.get("header") or {}).get("liveChatSponsorshipsHeaderRenderer")) or {}
        category, text = "member", _text(header.get("primaryText"))
        author = author or _text(header.get("authorName")).strip()
    elif kind == "liveChatSponsorshipsGiftRedemptionAnnouncementRenderer":
        category, text = "member", _text(renderer.get("message"))
    elif kind == "giftMessageViewModel":
        category, text = "member", _text(renderer.get("text"))
    else:
        return None
    text = " ".join(text.split())
    if not text and not amount:
        return None
    return {
        "id": str(renderer.get("id") or ""),
        "t": offset,
        "author": author[:80],
        "text": text[:MAX_TEXT],
        "kind": category,
        "amount": amount[:40],
        "role": _role(renderer),
    }


def parse_line(line: str) -> list[dict[str, Any]]:
    """Every chat message in one line of yt-dlp's live_chat JSON. Bad lines yield none."""
    try:
        data = json.loads(line)
    except (TypeError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    wrapper = data.get("replayChatItemAction") if isinstance(data.get("replayChatItemAction"), dict) else data
    raw_offset = data.get("videoOffsetTimeMsec", wrapper.get("videoOffsetTimeMsec"))
    try:
        offset = int(raw_offset) if raw_offset is not None else None
    except (TypeError, ValueError):
        offset = None
    messages = []
    for action in wrapper.get("actions") or []:
        item = ((action or {}).get("addChatItemAction") or {}).get("item") or {}
        for kind, renderer in item.items():
            message = _message(kind, renderer, offset)
            if message:
                messages.append(message)
    return messages


# --- Running yt-dlp ----------------------------------------------------------------------

def _chat_args() -> list[str]:
    """yt-dlp's shared flags without the operator's cookies: chat is public, and reading it
    must not happen as anyone's account."""
    args = ytdlp.base_args()
    while "--cookies" in args:
        index = args.index("--cookies")
        del args[index:index + 2]
    return args


def _die_with_parent() -> None:  # pragma: no cover - runs in the child between fork and exec
    """Linux: the follower dies with the thread that started it, so a live chat process
    never outlives the server or a reload."""
    try:
        import ctypes

        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG
    except Exception:
        pass


def _command(video_id: str, directory: str) -> list[str]:
    return [
        ytdlp.binary_path(), *_chat_args(), "--skip-download", "--write-subs", "--sub-langs", "live_chat",
        "--no-progress", "-o", os.path.join(directory, "chat"), "--", ytdlp.watch_url(video_id),
    ]


def _follow(video_id: str, on_messages: Callable[[list[dict[str, Any]]], None], should_stop: Callable[[], bool], timeout: float) -> str:
    """Run yt-dlp for one video's chat and hand over messages as the file grows.

    Returns "ended" when yt-dlp finished by itself, "stopped" when should_stop() asked,
    "timeout", or raises RuntimeError with yt-dlp's last error line.
    """
    directory = tempfile.mkdtemp(prefix="openfeed-chat-")
    part = os.path.join(directory, "chat.live_chat.json.part")
    final = os.path.join(directory, "chat.live_chat.json")
    handle = None
    pending = b""
    started = time.monotonic()
    outcome = "ended"
    errors_path = os.path.join(directory, "stderr.txt")
    try:
        # stderr goes to a file: a pipe nobody reads would stall a long live session.
        with open(errors_path, "wb") as errors_file:
            process = subprocess.Popen(
                _command(video_id, directory),
                stdout=subprocess.DEVNULL, stderr=errors_file, stdin=subprocess.DEVNULL,
                preexec_fn=_die_with_parent if os.name == "posix" else None,
            )

        def drain() -> None:
            nonlocal handle, pending
            if handle is None:
                for path in (part, final):
                    if os.path.exists(path):
                        handle = open(path, "rb")
                        break
                if handle is None:
                    return
            chunk = handle.read()
            if not chunk:
                return
            pending += chunk
            *lines, pending = pending.split(b"\n")
            batch: list[dict[str, Any]] = []
            for line in lines:
                if line.strip():
                    batch.extend(parse_line(line.decode("utf-8", "replace")))
            if batch:
                on_messages(batch)

        while process.poll() is None:
            drain()
            if should_stop():
                outcome = "stopped"
                break
            if time.monotonic() - started > timeout:
                outcome = "timeout"
                break
            time.sleep(0.5)
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        drain()
        if pending.strip():
            on_messages(parse_line(pending.decode("utf-8", "replace")))
            pending = b""
        with open(errors_path, "rb") as errors_file:
            error = errors_file.read()[-4000:].decode("utf-8", "replace").strip().splitlines()
        if outcome == "ended" and process.returncode not in (0, None) and handle is None:
            raise RuntimeError(next((line for line in reversed(error) if "ERROR" in line), error[-1] if error else f"yt-dlp exited {process.returncode}"))
        return outcome
    finally:
        if handle is not None:
            handle.close()
        shutil.rmtree(directory, ignore_errors=True)


# --- Status ------------------------------------------------------------------------------

def status(video_id: str) -> dict[str, Any]:
    """What chat this video has: live, replay or none, and why not when none."""
    if not ytdlp.available():
        return {"mode": "none", "reason": "yt-dlp is not installed on this server, and it is what reads chat."}
    try:
        info = ytdlp.probe(video_id)
    except ytdlp.YtdlpError as exc:
        message = str(exc)
        lowered = message.lower()
        if "premiere" in lowered or "will begin" in lowered or "upcoming" in lowered:
            return {"mode": "upcoming", "reason": "This stream or premiere has not started. Chat can be read once it is live."}
        return {"mode": "none", "reason": f"yt-dlp could not read this video: {message}"}
    live_status = str(info.get("live_status") or "")
    has_chat = "live_chat" in (info.get("subtitles") or {})
    if live_status == "is_live":
        return {"mode": "live", "reason": ""} if has_chat else {"mode": "none", "reason": "This stream has chat turned off."}
    if live_status in {"was_live", "post_live"}:
        if has_chat:
            return {"mode": "replay", "reason": ""}
        return {"mode": "none", "reason": "YouTube published no chat replay for this stream."}
    if live_status == "is_upcoming":
        return {"mode": "upcoming", "reason": "This stream or premiere has not started. Chat can be read once it is live."}
    return {"mode": "none", "reason": "Not a live stream or premiere."}


# --- Replay ------------------------------------------------------------------------------

_replays: OrderedDict[str, dict[str, Any]] = OrderedDict()
_replay_lock = threading.Lock()


def _run_replay(video_id: str, entry: dict[str, Any]) -> None:
    seen: set[str] = set()

    def take(batch: list[dict[str, Any]]) -> None:
        with _replay_lock:
            for message in batch:
                if message["t"] is None or (message["id"] and message["id"] in seen):
                    continue
                if len(entry["messages"]) >= MAX_REPLAY_MESSAGES:
                    entry["truncated"] = True
                    return
                if message["id"]:
                    seen.add(message["id"])
                entry["messages"].append(message)
                entry["through"] = max(entry["through"], int(message["t"]))

    try:
        outcome = _follow(video_id, take, lambda: entry.get("truncated", False), REPLAY_TIMEOUT)
        with _replay_lock:
            entry["messages"].sort(key=lambda message: message["t"])
            entry["state"] = "ready"
            entry["partial"] = outcome in {"timeout", "stopped"}
    except (OSError, RuntimeError) as exc:
        logger.info("chat replay for %s failed: %s", video_id, exc)
        with _replay_lock:
            entry["state"] = "error" if not entry["messages"] else "ready"
            entry["partial"] = bool(entry["messages"])
            entry["error"] = str(exc)[:300]
    finally:
        entry["finished"] = time.time()


def replay(video_id: str, start: float = 0.0, end: float | None = None) -> dict[str, Any]:
    """Messages with an offset in [start, end) seconds, starting the download if needed.

    While loading, `through` says how far into the video the messages already reach, so the
    page can show what it has and say that the rest is on its way.
    """
    with _replay_lock:
        entry = _replays.get(video_id)
        if entry is None or (entry["state"] == "error" and time.time() - entry["finished"] > REPLAY_RETRY_AFTER):
            entry = {"state": "loading", "messages": [], "through": 0, "partial": False, "error": "", "finished": 0.0}
            _replays[video_id] = entry
            while len(_replays) > REPLAY_CACHE_SIZE:
                oldest = next(iter(_replays))
                if _replays[oldest]["state"] == "loading":
                    break
                _replays.pop(oldest)
            threading.Thread(target=_run_replay, args=(video_id, entry), name=f"chat-replay-{video_id}", daemon=True).start()
        _replays.move_to_end(video_id)
        low = int(max(0.0, start) * 1000)
        high = int(end * 1000) if end is not None else None
        window = [
            message for message in entry["messages"]
            if message["t"] >= low and (high is None or message["t"] < high)
        ]
        if entry["state"] == "loading":
            window.sort(key=lambda message: message["t"])
        return {
            "state": entry["state"],
            "through": round(entry["through"] / 1000, 1),
            "partial": entry["partial"],
            "truncated": bool(entry.get("truncated")),
            "error": entry["error"],
            "count": len(entry["messages"]),
            "messages": window[:WINDOW_LIMIT],
        }


# --- Live --------------------------------------------------------------------------------

class _LiveSession:
    def __init__(self) -> None:
        self.messages: deque[dict[str, Any]] = deque(maxlen=LIVE_BUFFER)
        self.seq = 0
        self.seen: deque[str] = deque(maxlen=LIVE_BUFFER * 4)
        self.state = "starting"
        self.error = ""
        self.last_poll = time.monotonic()
        self.finished = 0.0


_live: dict[str, _LiveSession] = {}
_live_lock = threading.Lock()


def _run_live(video_id: str, session: _LiveSession) -> None:
    def take(batch: list[dict[str, Any]]) -> None:
        with _live_lock:
            session.state = "live"
            for message in batch:
                if message["id"] and message["id"] in session.seen:
                    continue
                if message["id"]:
                    session.seen.append(message["id"])
                session.seq += 1
                session.messages.append({**message, "seq": session.seq})

    def idle() -> bool:
        return time.monotonic() - session.last_poll > LIVE_IDLE_SECONDS

    try:
        outcome = _follow(video_id, take, idle, LIVE_MAX_SECONDS)
        session.state = "ended" if outcome == "ended" else "stopped"
    except (OSError, RuntimeError) as exc:
        logger.info("live chat for %s failed: %s", video_id, exc)
        session.state = "error"
        session.error = str(exc)[:300]
    finally:
        session.finished = time.monotonic()


def live(video_id: str, after: int = 0) -> dict[str, Any]:
    """Messages newer than `after` (a sequence number from an earlier answer)."""
    with _live_lock:
        session = _live.get(video_id)
        restart = session is None or session.state == "stopped" or (
            session.state == "error" and time.monotonic() - session.finished > REPLAY_RETRY_AFTER
        )
        if restart:
            running = sum(1 for item in _live.values() if item.state in {"starting", "live"})
            if running >= LIVE_MAX_SESSIONS:
                return {"state": "busy", "error": f"{LIVE_MAX_SESSIONS} live chats are already being followed on this server.", "seq": after, "messages": []}
            session = _LiveSession()
            _live[video_id] = session
            threading.Thread(target=_run_live, args=(video_id, session), name=f"chat-live-{video_id}", daemon=True).start()
        session.last_poll = time.monotonic()
        fresh = [message for message in session.messages if message["seq"] > after]
        # A client that fell far behind (or a first poll) gets the newest slice, not a flood.
        return {"state": session.state, "error": session.error, "seq": session.seq, "messages": fresh[-LIVE_BATCH:]}


def reset() -> None:
    """Forget cached replays and sessions. Tests only; running followers stop by idling out."""
    with _replay_lock:
        _replays.clear()
    with _live_lock:
        _live.clear()
