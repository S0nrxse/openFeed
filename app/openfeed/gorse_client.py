from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

from .config import get_settings

POSITIVE_EVENTS = {
    "like": "like",
    "more_like_this": "like",
    "mark_watched": "complete",
    "complete": "complete",
    "rewatch": "rewatch",
    "save": "save",
    "subscribe": "subscribe",
}
NEGATIVE_EVENTS = {
    "dislike": "dislike",
    "less_like_this": "dislike",
    "hide": "not_interested",
    "not_interested": "not_interested",
}
READ_EVENTS = {
    "watch_page_open": "click",
    "open": "click",
    "progress": "watch_progress",
    "watch_progress": "watch_progress",
    "watch": "watch",
}
DELETE_EVENTS = {
    "clear_reaction": ("like", "dislike"),
    "undo_hide": ("not_interested", "hide"),
    "undo_not_interested": ("not_interested", "hide"),
}


def configured() -> bool:
    settings = get_settings()
    return bool(settings.gorse_base_url)


def _headers() -> dict[str, str]:
    settings = get_settings()
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if settings.gorse_server_api_key:
        headers["X-API-Key"] = settings.gorse_server_api_key
    return headers


def _url(path: str) -> str:
    return f"{get_settings().gorse_base_url.rstrip('/')}{path}"


def _request(method: str, path: str, *, json: Any | None = None, timeout: float = 4.0) -> Any | None:
    if not configured():
        return None
    try:
        response = httpx.request(method, _url(path), headers=_headers(), json=json, timeout=timeout)
        if response.status_code >= 400:
            return None
        if response.text:
            return response.json()
        return {}
    except Exception:
        return None


def _timestamp(value: Any = None) -> str:
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _labels_for_video(video: dict[str, Any]) -> list[str]:
    labels: list[str] = []
    for value in (
        video.get("source_kind"),
        video.get("channel_title"),
        video.get("author"),
        video.get("youtube_channel_id"),
    ):
        text = str(value or "").strip().lower()
        if text and text not in labels:
            labels.append(text[:80])
    for token in video.get("topic_tokens") or []:
        text = str(token or "").strip().lower()
        if text and text not in labels:
            labels.append(text[:80])
    return labels[:32]


def upsert_user(user_key: str, labels: list[str] | None = None) -> bool:
    if not user_key:
        return False
    payload = {"UserId": user_key, "Labels": labels or []}
    return _request("POST", "/api/user", json=payload) is not None


def upsert_item(video: dict[str, Any]) -> bool:
    item_id = str(video.get("youtube_video_id") or "").strip()
    if not item_id:
        return False
    payload: dict[str, Any] = {
        "ItemId": item_id,
        "Labels": _labels_for_video(video),
        "Comment": str(video.get("title") or "")[:1000],
        "Categories": [str(video.get("source_kind") or "video")],
    }
    published_at = video.get("published_at")
    if published_at:
        payload["Timestamp"] = _timestamp(published_at)
    return _request("POST", "/api/item", json=payload) is not None


def insert_feedback(user_key: str, item_id: str, feedback_type: str, *, value: float = 0.0, comment: str = "") -> bool:
    if not user_key or not item_id or not feedback_type:
        return False
    payload = [{
        "FeedbackType": feedback_type,
        "UserId": user_key,
        "ItemId": item_id,
        "Value": value,
        "Timestamp": _timestamp(),
        "Comment": comment[:1000],
    }]
    return _request("POST", "/api/feedback", json=payload) is not None


def delete_feedback(user_key: str, item_id: str, feedback_type: str) -> bool:
    if not user_key or not item_id or not feedback_type:
        return False
    return _request("DELETE", f"/api/feedback/{feedback_type}/{user_key}/{item_id}") is not None


def sync_feedback_event(user_key: str, video: dict[str, Any] | None, event_type: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    if not video:
        return {"ok": False, "reason": "video_missing"}
    item_id = str(video.get("youtube_video_id") or "").strip()
    if not item_id or not user_key:
        return {"ok": False, "reason": "ids_missing"}
    payload = payload or {}
    upsert_user(user_key)
    upsert_item(video)
    if event_type in DELETE_EVENTS:
        deleted = [delete_feedback(user_key, item_id, feedback_type) for feedback_type in DELETE_EVENTS[event_type]]
        return {"ok": any(deleted), "deleted": deleted}
    feedback_type = POSITIVE_EVENTS.get(event_type) or NEGATIVE_EVENTS.get(event_type) or READ_EVENTS.get(event_type)
    if not feedback_type:
        return {"ok": False, "reason": "unsupported_event"}
    value = 0.0
    if event_type in {"progress", "watch_progress", "watch"}:
        try:
            value = float(payload.get("percent_completed") or payload.get("percent") or 0.0)
        except (TypeError, ValueError):
            value = 0.0
        if value >= 0.65:
            feedback_type = "watch>=0.65"
    ok = insert_feedback(user_key, item_id, feedback_type, value=value, comment=event_type)
    return {"ok": ok, "feedback_type": feedback_type}


def recommend(user_key: str, limit: int = 50) -> list[str]:
    if not user_key:
        return []
    n = max(1, min(int(limit or 50), 200))
    payload = _request("GET", f"/api/recommend/{user_key}?n={n}", timeout=3.0)
    if not isinstance(payload, list):
        return []
    items: list[str] = []
    for item in payload:
        if isinstance(item, str):
            item_id = item
        elif isinstance(item, dict):
            item_id = str(item.get("ItemId") or item.get("item_id") or "")
        else:
            item_id = ""
        if item_id and item_id not in items:
            items.append(item_id)
    return items
