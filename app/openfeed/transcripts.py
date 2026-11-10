"""On-demand transcript text for one video, as data: `GET /api/transcript/{id}/fetch`.

An outside tool (NOAH's `myyt_fetch_transcript`) reads this endpoint, so its request and
response are a contract: the field names, the `status` values and the error `code`s below
must not change. It differs from the player's captions (captions.py) in what it is for:
the player needs WebVTT it can play, from any source that has one; this returns timed
text segments, from the configured Invidious only, with explicit language and
manual/automatic metadata, hard size limits and an explicit outcome instead of an empty
success.

What it shares with captions.py, rather than doing twice:

* the caption-file route. When Invidious' transcript API fails, the fallback reads the
  same track's caption file the way the player does: through the signed companion URL in
  Invidious' embed page (`captions.embed_track_sources`), which keeps working while
  YouTube rate-limits Invidious' direct caption fetches;
* the WebVTT reader. The file goes through `captions.clean_vtt` and
  `captions.parse_cues`, which also collapse YouTube's "typed out, then scrolled up"
  automatic captions so each line appears once;
* the small expiring cache, which keeps successful results for half an hour (`cached`
  says when an answer came from it).

Where it talks to: only `INVIDIOUS_BASE_URL`, the Invidious this install is configured
with (it used to be a hard-coded host). At most four requests per video (track list,
transcript; on a provider error, the embed page that signs the caption file and the
file), no redirects, no cookies, identity encoding only, 2 MiB per response, a
25-second deadline overall. Provider-supplied URLs in the API answers are ignored; the
one URL taken from a page is the embed page's signed caption path, and only on the
configured host.
"""

from __future__ import annotations

import asyncio
import json
import re
import urllib.parse
from datetime import datetime, timezone
from html import unescape
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from . import captions
from .config import get_settings

MAX_BYTES = 2 * 1024 * 1024
MAX_SEGMENTS = 5000
MAX_TEXT = 250_000
MAX_TRACKS = 100
TOTAL_TIMEOUT = 25
RESULT_TTL = 1800.0
LANGUAGE = r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8}){0,3}$"

_results = captions._TTLCache(64)


class TranscriptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    youtube_video_id: str = Field(pattern=r"^[A-Za-z0-9_-]{11}$", min_length=11, max_length=11)
    language: str = Field(default="en", pattern=LANGUAGE, max_length=35)
    kind: Literal["prefer_manual", "manual", "auto"] = "prefer_manual"
    list_only: bool = False


class TranscriptError(Exception):
    """One failed outcome. `code` is part of the endpoint's contract."""

    def __init__(self, code: str, provider_status: int | None = None):
        self.code = code
        self.provider_status = provider_status
        super().__init__(code)


def clear_cache() -> None:
    _results.clear()


def provider_base() -> str:
    """The configured Invidious origin, or "" when it is missing or not a plain http(s) origin.

    The only host this module ever contacts. Paths, queries, credentials and fragments are
    refused rather than stripped, so a typo cannot quietly point it somewhere else.
    """
    raw = str(get_settings().invidious_base_url or "").strip().rstrip("/")
    try:
        parts = urllib.parse.urlsplit(raw)
        parts.port  # noqa: B018 - raises ValueError on a malformed port
    except ValueError:
        return ""
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return ""
    if parts.username or parts.password or parts.path or parts.query or parts.fragment:
        return ""
    return raw


async def _body(client: httpx.AsyncClient, url: str, params: dict[str, str] | None, media: str) -> bytes:
    # Clear even service-issued cookies between requests; no credential delegation.
    client.cookies.clear()
    async with client.stream("GET", url, params=params) as response:
        status = response.status_code
        if status != 200:
            code = {
                404: "caption_unavailable", 429: "caption_provider_rate_limited",
                401: "caption_provider_blocked", 403: "caption_provider_blocked",
            }.get(status, "caption_provider_error")
            if 300 <= status < 400:
                code = "caption_provider_redirect_refused"
            raise TranscriptError(code, status)
        if response.headers.get("content-type", "").split(";")[0].strip() != media:
            raise TranscriptError("caption_provider_invalid_response")
        if response.headers.get("content-encoding", "identity").lower() != "identity":
            raise TranscriptError("caption_provider_invalid_response")
        body = bytearray()
        async for chunk in response.aiter_raw():
            if len(body) + len(chunk) > MAX_BYTES:
                raise TranscriptError("caption_response_too_large")
            body.extend(chunk)
    return bytes(body)


async def _json(client: httpx.AsyncClient, url: str, params: dict[str, str]) -> dict[str, Any]:
    body = await _body(client, url, params, "application/json")
    try:
        result = json.loads(body, parse_constant=lambda _: None)
    except (ValueError, UnicodeError, RecursionError):
        raise TranscriptError("caption_provider_invalid_response") from None
    if not isinstance(result, dict):
        raise TranscriptError("caption_provider_invalid_response")
    return result


def tracks_from_listing(payload: dict[str, Any]) -> list[dict[str, str]]:
    """{language, kind, label} for each track Invidious lists. Its URLs are ignored."""
    rows = payload.get("transcripts")
    if not isinstance(rows, list) or len(rows) > MAX_TRACKS:
        raise TranscriptError("caption_provider_invalid_response")
    tracks = []
    for row in rows:
        if not isinstance(row, dict):
            raise TranscriptError("caption_provider_invalid_response")
        language, auto, label = row.get("languageCode"), row.get("autoGenerated"), row.get("label")
        if (not isinstance(language, str) or not re.fullmatch(LANGUAGE, language)
                or type(auto) is not bool or not isinstance(label, str) or len(label) > 256):
            raise TranscriptError("caption_provider_invalid_response")
        tracks.append({"language": language, "kind": "auto" if auto else "manual", "label": label})
    return tracks


def segments_from_transcript(payload: dict[str, Any], selected: dict[str, str]) -> list[dict[str, Any]]:
    """Validated segments from Invidious' transcript JSON for the selected track."""
    transcript = payload.get("transcript")
    if not isinstance(transcript, dict):
        raise TranscriptError("caption_provider_invalid_response")
    if (transcript.get("languageCode") != selected["language"]
            or type(transcript.get("autoGenerated")) is not bool
            or transcript["autoGenerated"] != (selected["kind"] == "auto")):
        raise TranscriptError("caption_track_mismatch")
    rows = transcript.get("body")
    if not isinstance(rows, list):
        raise TranscriptError("caption_provider_invalid_response")
    if len(rows) > MAX_SEGMENTS:
        raise TranscriptError("caption_segment_limit_exceeded")
    segments = []
    total = 0
    previous = -1.0
    for row in rows:
        if not isinstance(row, dict):
            raise TranscriptError("caption_provider_invalid_response")
        start, end, text = row.get("startMs"), row.get("endMs"), row.get("line")
        if (type(start) not in (int, float) or type(end) not in (int, float)
                or not 0 <= start <= end <= 604_800_000 or start < previous
                or not isinstance(text, str) or len(text) > 10_000
                or row.get("type") not in ("regular", "heading")):
            raise TranscriptError("caption_provider_invalid_response")
        previous = start
        total += len(text)
        if total > MAX_TEXT:
            raise TranscriptError("caption_text_limit_exceeded")
        if text.strip():
            segments.append({"start_seconds": start / 1000, "end_seconds": end / 1000,
                             "text": text, "type": row["type"]})
    if not segments:
        raise TranscriptError("caption_empty")
    return segments


_TAG = re.compile(r"<[^>]*>")


def segments_from_vtt(body: bytes, selected: dict[str, str]) -> list[dict[str, Any]]:
    """Segments from a WebVTT caption file, read with the player's own parser.

    captions.clean_vtt drops styling and inline word timings and, for automatic captions,
    the duplicate scroll-up cues; what is left goes through the same validation and limits
    as the JSON path, so both paths return the same shape.
    """
    try:
        text = body.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
    except UnicodeError:
        raise TranscriptError("caption_provider_invalid_response") from None
    if not re.match(r"WEBVTT(?:[ \t][^\n]*)?(?:\n|$)", text.lstrip("\n")):
        raise TranscriptError("caption_provider_invalid_response")
    if text.count("-->") > MAX_SEGMENTS * 3:
        raise TranscriptError("caption_segment_limit_exceeded")
    cues = captions.parse_cues(captions.clean_vtt(text, auto=selected["kind"] == "auto"))
    if len(cues) > MAX_SEGMENTS:
        raise TranscriptError("caption_segment_limit_exceeded")
    rows = [
        {"startMs": round(start * 1000), "endMs": round(end * 1000),
         "line": unescape(_TAG.sub("", "\n".join(lines))), "type": "regular"}
        for start, end, _settings, lines in cues
    ]
    rows.sort(key=lambda row: row["startMs"])
    return segments_from_transcript(
        {"transcript": {"languageCode": selected["language"], "autoGenerated": selected["kind"] == "auto", "body": rows}},
        selected,
    )


async def _caption_file_url(client: httpx.AsyncClient, base: str, video_id: str, label: str) -> tuple[str, dict[str, str] | None]:
    """Where to read the caption file for one track: (url, params).

    The same route the player takes (captions.py): the signed companion URL that
    Invidious' own embed page puts in its <track> elements, because Invidious' direct
    caption fetch is the hop YouTube rate-limits first. The direct URL is used only when
    the embed page names no signed source for this label. Either way the host is `base`.
    """
    try:
        page = await _body(client, f"{base}/embed/{video_id}", None, "text/html")
        signed = captions.embed_track_sources(page.decode("utf-8", "replace")).get(label, "")
    except TranscriptError:
        signed = ""
    if signed:
        path = signed if signed.startswith("/") else f"/{signed}"
        url = base + captions._browser_encode(path)
        if urllib.parse.urlsplit(url).netloc == urllib.parse.urlsplit(base).netloc:
            return url, None
    return f"{base}/api/v1/captions/{video_id}", {"label": label}


def pick(tracks: list[dict[str, str]], language: str, kind: str) -> dict[str, str]:
    """The exact-language track of the requested kind; prefer_manual takes auto only when
    no manual one exists. Never another language."""
    candidates = [track for track in tracks if track["language"].lower() == language.lower()
                  and (kind == "prefer_manual" or track["kind"] == kind)]
    if not candidates:
        raise TranscriptError("caption_track_unavailable")
    return sorted(candidates, key=lambda track: track["kind"] != "manual")[0]


async def fetch(request: TranscriptRequest) -> dict[str, Any]:
    """Return an explicit retrieval outcome, never an empty successful transcript."""
    # Revalidate even when called outside FastAPI.
    request = TranscriptRequest.model_validate(request.model_dump())
    video_id = request.youtube_video_id
    common = {"youtube_video_id": video_id, "provider": "invidious",
              "source_url": f"https://www.youtube.com/watch?v={video_id}"}
    cache_key = (video_id, request.language.lower(), request.kind)
    if not request.list_only:
        cached = _results.get(cache_key)
        if cached is not None:
            return {**common, **cached, "cached": True}
    tracks: list[dict[str, str]] = []
    stage = "track_list"
    try:
        base = provider_base()
        if not base:
            raise TranscriptError("caption_provider_not_configured")
        url = f"{base}/api/v1/transcripts/{video_id}"
        async with asyncio.timeout(TOTAL_TIMEOUT):
            async with httpx.AsyncClient(timeout=httpx.Timeout(10, connect=3),
                                         follow_redirects=False, trust_env=False,
                                         headers={"Accept": "application/json", "Accept-Encoding": "identity"}) as client:
                tracks = tracks_from_listing(await _json(client, url, {}))
                if not tracks:
                    raise TranscriptError("no_captions")
                if request.list_only:
                    return {**common, "ok": True, "status": "tracks", "available_tracks": tracks}
                selected = pick(tracks, request.language, request.kind)
                params = {"lang": selected["language"]}
                # Presence of autogen selects automatic captions, even autogen=false.
                if selected["kind"] == "auto":
                    params["autogen"] = ""
                provenance = "invidious_youtube_transcripts_api"
                stage = "transcript_text"
                try:
                    segments = segments_from_transcript(await _json(client, url, params), selected)
                except TranscriptError as exc:
                    # One bounded alternate representation of the same selected track.
                    # Never retry a rate limit, auth block, redirect, or malformed body.
                    if exc.code not in {"caption_provider_error", "caption_unavailable"}:
                        raise
                    if any(track["label"] == selected["label"] and track != selected for track in tracks):
                        raise TranscriptError("caption_track_ambiguous") from None
                    stage = "caption_vtt"
                    body = await _body(client, *(await _caption_file_url(client, base, video_id, selected["label"])), "text/vtt")
                    segments = segments_from_vtt(body, selected)
                    provenance = "invidious_youtube_captions_vtt"
        outcome = {"ok": True, "status": "fetched", "track": selected,
                   "available_tracks": tracks, "segments": segments, "count": len(segments),
                   "fetched_at": datetime.now(timezone.utc).isoformat(),
                   "provenance": provenance, "time_unit": "seconds"}
        _results.put(cache_key, outcome, RESULT_TTL)
        return {**common, **outcome, "cached": False}
    except (TimeoutError, httpx.TimeoutException):
        error = TranscriptError("caption_provider_timeout")
    except httpx.HTTPError:
        error = TranscriptError("caption_provider_transport_error")
    except TranscriptError as exc:
        error = exc
    return {**common, "ok": False, "status": "unavailable", "code": error.code,
            "provider_status": error.provider_status, "stage": stage, "available_tracks": tracks}
