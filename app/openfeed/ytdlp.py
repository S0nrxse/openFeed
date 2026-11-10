"""yt-dlp as a fallback media backend.

Invidious remains the primary source for browsing, search, channels, comments,
recommendations and normal playback. Nothing in this module sits on the browse path.
yt-dlp is reached for in exactly two situations:

* Invidious cannot produce a usable stream — it returned nothing playable, or the
  stream it handed back answers with 403 because the PO token was rejected.
* A download asks for a rendition above 720p, which YouTube only publishes as separate
  video and audio tracks that have to be muxed back together.

Extraction runs the binary in metadata-only mode and parses its JSON. It is deliberately
a subprocess rather than the Python API: yt-dlp is the component most likely to hang on
a network stall or die on a YouTube change, and a subprocess can be given a hard timeout
and killed without taking a worker thread with it.
"""

from __future__ import annotations

import json
import logging
import re
import shlex
import shutil
import subprocess
import threading
import time
from typing import Any

from . import outbound
from .config import get_settings

logger = logging.getLogger("openfeed.ytdlp")


class YtdlpError(RuntimeError):
    """yt-dlp could not produce metadata for a video."""


def watch_url(youtube_video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={youtube_video_id}"


def binary_path() -> str:
    """The resolved yt-dlp executable, or "" when it is not installed."""
    configured = get_settings().ytdlp_binary or "yt-dlp"
    return shutil.which(configured) or ""


def available() -> bool:
    return bool(get_settings().ytdlp_enabled and binary_path())


def ffmpeg_available() -> bool:
    """Merging separate video and audio tracks is impossible without ffmpeg."""
    return bool(shutil.which("ffmpeg"))


def version() -> str:
    path = binary_path()
    if not path:
        return ""
    try:
        result = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def base_args() -> list[str]:
    """Flags shared by extraction and downloading.

    The cookies file is passed by path and never read here, so a credential the operator
    supplies stays out of this process's memory and out of the logs.
    """
    settings = get_settings()
    args = ["--no-playlist", "--no-warnings", "--ignore-config", *outbound.ytdlp_args()]
    if settings.ytdlp_cookies_file:
        args += ["--cookies", settings.ytdlp_cookies_file]
    if settings.ytdlp_extra_args:
        try:
            extra = shlex.split(settings.ytdlp_extra_args)
        except ValueError:
            logger.warning("OPENFEED_YTDLP_EXTRA_ARGS is not parseable as shell words; ignoring it")
            extra = []
        # These flags execute commands, load local files into the process or swap the
        # helper binaries: none of these belong in an env-passed flag string.
        # - --exec* runs an arbitrary shell command per video
        # - --load-info* reads local JSON into the process, --config-location loads
        #   option files from disk
        # - --use-postprocessor / --postprocessor-args reach ExecAfterDownload's exec_cmd
        # - --external-downloader* swaps the fetch binary, --ffmpeg-location points at an
        #   arbitrary binary in ffmpeg's place, --downloader* is the external-downloader alias
        # - --netrc-cmd runs a command to fetch credentials
        blocked_prefixes = (
            "--exec",
            "--load-info",
            "--config-location",
            "--use-postprocessor",
            "--postprocessor",
            "--external-downloader",
            "--downloader",
            "--ffmpeg-location",
            "--netrc-cmd",
        )
        filtered: list[str] = []
        index = 0
        while index < len(extra):
            token = extra[index]
            name = token.split("=", 1)[0]
            if any(name == prefix or name.startswith(prefix) for prefix in blocked_prefixes):
                logger.warning("ignoring blocked yt-dlp extra arg: %s", name)
                index += 1
                # Bare `--flag value` form: drop the flag's value token(s) too, since
                # an --exec command is typically multiple words (`--exec echo hi`).
                if "=" not in token:
                    while index < len(extra) and not extra[index].startswith("-"):
                        index += 1
                continue
            filtered.append(token)
            index += 1
        args += filtered
    return args


# Extracted URLs are signed and expire, so this cache is sized to collapse a burst of
# requests for one video, not to keep anything around.
_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_cache_lock = threading.Lock()


def _cache_get(video_id: str) -> dict[str, Any] | None:
    with _cache_lock:
        entry = _cache.get(video_id)
        if not entry:
            return None
        expires_at, payload = entry
        if time.time() >= expires_at:
            _cache.pop(video_id, None)
            return None
        return payload


def _cache_put(video_id: str, payload: dict[str, Any]) -> None:
    ttl = max(0, get_settings().ytdlp_cache_seconds)
    if not ttl:
        return
    with _cache_lock:
        _cache[video_id] = (time.time() + ttl, payload)
        if len(_cache) > 128:
            now = time.time()
            for key, (expires_at, _) in list(_cache.items()):
                if expires_at <= now:
                    _cache.pop(key, None)
            while len(_cache) > 128:
                _cache.pop(next(iter(_cache)), None)


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def probe(youtube_video_id: str, *, force: bool = False) -> dict[str, Any]:
    """Metadata-only extraction for one video. Never downloads anything.

    Raises YtdlpError when the binary is missing, times out, or cannot parse the video.
    """
    if not available():
        raise YtdlpError("yt-dlp is not available")
    if not force:
        cached = _cache_get(youtube_video_id)
        if cached is not None:
            return cached

    settings = get_settings()
    command = [binary_path(), "-J", *base_args(), "--", watch_url(youtube_video_id)]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=max(10, settings.ytdlp_timeout_seconds),
        )
    except subprocess.TimeoutExpired as exc:
        raise YtdlpError(f"yt-dlp timed out after {settings.ytdlp_timeout_seconds}s") from exc
    except OSError as exc:
        raise YtdlpError(f"could not run yt-dlp: {exc}") from exc

    if result.returncode != 0:
        detail = (result.stderr or "").strip().splitlines()
        raise YtdlpError(detail[-1] if detail else f"yt-dlp exited {result.returncode}")
    try:
        info = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise YtdlpError("yt-dlp returned output that was not JSON") from exc

    _cache_put(youtube_video_id, info)
    return info


def _height(fmt: dict[str, Any]) -> int:
    try:
        return int(fmt.get("height") or 0)
    except (TypeError, ValueError):
        return 0


_RESOLUTION_NOTE = re.compile(r"^(\d{3,4})p(\d{2,3})?$")


def resolution_label(fmt: dict[str, Any]) -> tuple[str, int]:
    """What to call a rendition, and the number behind that name.

    `height` is the true pixel height, which for a portrait video is the *long* edge — a
    1080x1920 short would otherwise be offered as "1920p". yt-dlp's `format_note` already
    carries what YouTube calls the rendition, including the frame rate suffix on "1080p60",
    so it is trusted whenever it looks like a resolution; the short edge is the fallback.
    """
    note = str(fmt.get("format_note") or "").strip()
    match = _RESOLUTION_NOTE.match(note)
    if match:
        return note, int(match.group(1))
    height = _height(fmt)
    width = int(fmt.get("width") or 0)
    short = min(width, height) if width and height else height
    return (f"{short}p" if short else ""), short


def _is_video(fmt: dict[str, Any]) -> bool:
    return str(fmt.get("vcodec") or "none") != "none"


def _is_audio(fmt: dict[str, Any]) -> bool:
    return str(fmt.get("acodec") or "none") != "none"


def _size_of(fmt: dict[str, Any]) -> int:
    size = fmt.get("filesize") or fmt.get("filesize_approx")
    return int(size) if isinstance(size, (int, float)) and size else 0


# yt-dlp's own preference among video codecs, best first. Sizing has to follow the same
# order the format selector does, or the picker quotes one rendition and downloads
# another — at 1080p the h264 track is more than twice the size of the av1 one.
_VCODEC_RANK = ("av01", "av1", "vp9.2", "vp09", "vp9", "hev1", "hvc1", "h265", "avc1", "h264")


def _vcodec_rank(fmt: dict[str, Any]) -> int:
    codec = str(fmt.get("vcodec") or "").lower()
    for index, name in enumerate(_VCODEC_RANK):
        if codec.startswith(name):
            return index
    return len(_VCODEC_RANK)


def selector_pick(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    """The format `bv*[ext=mp4]` will actually resolve to, so its size can be quoted.

    Mirrors the two things that decide it in practice: the mp4 branch of the selector
    comes first, and within one resolution yt-dlp ranks by codec before bitrate.
    """
    mp4 = [fmt for fmt in candidates if str(fmt.get("ext") or "") == "mp4"]
    return sorted(mp4 or candidates, key=lambda fmt: (_vcodec_rank(fmt), -(fmt.get("tbr") or 0)))[0]


def best_audio(info: dict[str, Any]) -> dict[str, Any] | None:
    """The track `ba[ext=m4a]` would pair with a split video rendition."""
    audio = [
        fmt for fmt in info.get("formats") or []
        if fmt.get("url") and _is_audio(fmt) and not _is_video(fmt)
    ]
    if not audio:
        return None
    m4a = [fmt for fmt in audio if str(fmt.get("ext") or "") == "m4a"]
    return max(m4a or audio, key=lambda fmt: fmt.get("tbr") or 0)


def progressive_streams(info: dict[str, Any]) -> list[dict[str, Any]]:
    """Formats carrying both picture and sound, best first.

    Only these can drive a plain <video src>. The higher renditions are split into
    separate tracks and would need MSE on the client or a mux on the server, neither of
    which belongs in a fallback whose whole job is to get *something* playing.
    """
    streams = [
        fmt for fmt in info.get("formats") or []
        if fmt.get("url") and _is_video(fmt) and _is_audio(fmt)
    ]
    streams.sort(key=lambda fmt: (_height(fmt), fmt.get("tbr") or 0), reverse=True)
    return streams


def playback_stream(youtube_video_id: str) -> dict[str, Any]:
    """A directly playable stream for the fallback player.

    Raises YtdlpError when the video has no progressive rendition at all, which is rare
    but real — some live and members-only videos ship split tracks exclusively.
    """
    info = probe(youtube_video_id)
    streams = progressive_streams(info)
    if not streams:
        raise YtdlpError("yt-dlp found no stream carrying both video and audio")
    best = streams[0]
    extension = str(best.get("ext") or "mp4")
    label, label_height = resolution_label(best)
    return {
        "youtube_video_id": youtube_video_id,
        "title": info.get("title") or youtube_video_id,
        "stream_url": best.get("url"),
        "stream_type": f"video/{extension}",
        "quality": label,
        "height": label_height,
        "format_id": str(best.get("format_id") or ""),
        "duration": info.get("duration"),
        "source": "yt-dlp",
    }


def download_formats(youtube_video_id: str) -> dict[str, Any]:
    """Every resolution a merged download could produce, best first.

    A height qualifies if there is a video track at it and some audio track exists to
    pair with, so 1440p and 2160p appear here even though no single file at those
    resolutions is downloadable — that is exactly what the ffmpeg merge is for.
    """
    settings = get_settings()
    info = probe(youtube_video_id)
    audio = best_audio(info)
    audio_size = _size_of(audio) if audio else 0
    mergeable = bool(audio) and ffmpeg_available()
    progressive = {resolution_label(fmt)[1] for fmt in progressive_streams(info)}

    # Grouped by the label rather than by raw height, because several real heights can
    # share one name — YouTube ships both 480x854 and 608x1080 renditions as "480p".
    groups: dict[int, list[dict[str, Any]]] = {}
    for fmt in info.get("formats") or []:
        if not fmt.get("url") or not _is_video(fmt):
            continue
        _, label_height = resolution_label(fmt)
        if not label_height or label_height > settings.download_max_height:
            continue
        # A split video track is only offerable if we can pair audio with it and merge.
        if not _is_audio(fmt) and not mergeable:
            continue
        groups.setdefault(label_height, []).append(fmt)

    options: list[dict[str, Any]] = []
    for label_height, candidates in groups.items():
        chosen = selector_pick(candidates)
        needs_merge = not _is_audio(chosen)
        size = _size_of(chosen)
        # A merge downloads both tracks, so quoting the video track alone understates it.
        if size and needs_merge:
            size += audio_size
        options.append({
            "label_height": label_height,
            # The real pixel height, which is what `-f bv*[height<=N]` filters on. Passing
            # the label here would pick a far smaller rendition for portrait video, and it
            # has to cover the tallest variant sharing this label.
            "height": max(_height(fmt) for fmt in candidates),
            "quality": resolution_label(chosen)[0],
            "ext": str(chosen.get("ext") or "mp4"),
            "needs_merge": needs_merge,
            "size_bytes": size or None,
            # yt-dlp's ranking has more terms than the two mirrored here, so a merged size
            # is close rather than exact and the UI says so.
            "size_estimated": needs_merge,
            "vcodec": str(chosen.get("vcodec") or ""),
            # A rendition Invidious already serves muxed does not need ffmpeg at all.
            "progressive": label_height in progressive,
        })

    options.sort(key=lambda item: item["label_height"], reverse=True)
    return {
        "youtube_video_id": youtube_video_id,
        "title": info.get("title") or youtube_video_id,
        "duration": info.get("duration"),
        "formats": options,
        "ffmpeg": ffmpeg_available(),
    }
