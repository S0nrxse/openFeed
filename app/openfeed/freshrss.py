from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

from .config import Settings
from .youtube_ids import youtube_channel_id_from_text, youtube_video_id_from_url

READ_CATEGORY = "/state/com.google/read"
STAR_CATEGORY = "/state/com.google/starred"


class FreshRSSConfigError(RuntimeError):
    pass


class FreshRSSError(RuntimeError):
    pass


class FreshRSSClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        if not settings.freshrss_configured:
            raise FreshRSSConfigError(
                "FreshRSS is not configured. Set FRESHRSS_USERNAME and FRESHRSS_API_PASSWORD in the OpenFeed environment."
            )
        self.base_url = settings.freshrss_base_url.rstrip("/")
        self._auth: str | None = None

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def authenticate(self, client: httpx.Client) -> str:
        response = client.post(
            self._url("/api/greader.php/accounts/ClientLogin"),
            data={"Email": self.settings.freshrss_username, "Passwd": self.settings.freshrss_api_password},
        )
        if response.status_code >= 400:
            raise FreshRSSError(f"FreshRSS auth failed with HTTP {response.status_code}: {response.text[:200]}")
        fields: dict[str, str] = {}
        for line in response.text.splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                fields[key] = value
        auth = fields.get("Auth")
        if not auth:
            raise FreshRSSError("FreshRSS auth response did not include an Auth token")
        self._auth = auth
        return auth

    def _headers(self, client: httpx.Client) -> dict[str, str]:
        auth = self._auth or self.authenticate(client)
        return {"Authorization": f"GoogleLogin auth={auth}"}

    def get_json(self, client: httpx.Client, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        response = client.get(self._url(path), params=params or {}, headers=self._headers(client))
        if response.status_code >= 400:
            raise FreshRSSError(f"FreshRSS GET {path} failed with HTTP {response.status_code}: {response.text[:200]}")
        return response.json()

    def get_text(self, client: httpx.Client, path: str, params: dict[str, Any] | None = None) -> str:
        response = client.get(self._url(path), params=params or {}, headers=self._headers(client))
        if response.status_code >= 400:
            raise FreshRSSError(f"FreshRSS GET {path} failed with HTTP {response.status_code}: {response.text[:200]}")
        return response.text

    def token(self, client: httpx.Client) -> str | None:
        token = self.get_text(client, "/api/greader.php/reader/api/0/token").strip()
        return token or None

    def post_form(self, client: httpx.Client, path: str, data: dict[str, Any]) -> str:
        response = client.post(self._url(path), data=data, headers=self._headers(client))
        if response.status_code >= 400:
            raise FreshRSSError(f"FreshRSS POST {path} failed with HTTP {response.status_code}: {response.text[:200]}")
        return response.text

    def subscribe_feed(self, feed_url: str, title: str | None = None) -> None:
        cleaned = feed_url.strip()
        if not cleaned:
            raise FreshRSSError("FreshRSS subscription requires a feed URL")
        stream_id = cleaned if cleaned.startswith("feed/") else f"feed/{cleaned}"
        with httpx.Client(timeout=30.0, follow_redirects=True) as client:
            data: dict[str, Any] = {"ac": "subscribe", "s": stream_id}
            if title:
                data["t"] = title
            try:
                token = self.token(client)
            except FreshRSSError:
                token = None
            if token:
                data["T"] = token
            self.post_form(client, "/api/greader.php/reader/api/0/subscription/edit", data)

    def stream_id_for_feed_url(self, feed_url: str) -> str | None:
        """Resolve a feed URL to its FreshRSS stream id (``feed/<n>``)."""
        try:
            for subscription in self.subscriptions():
                if str(subscription.get("url") or "").strip() == feed_url:
                    stream_id = str(subscription.get("id") or "").strip()
                    if stream_id:
                        return stream_id
        except FreshRSSError:
            return None
        return None

    def unsubscribe_feed(self, feed_url: str) -> None:
        cleaned = feed_url.strip()
        if not cleaned:
            raise FreshRSSError("FreshRSS unsubscribe requires a feed URL")
        # Unlike subscribe, GReader's unsubscribe will not accept a feed URL as the
        # stream id — it needs the existing subscription's id, so look it up first.
        if cleaned.startswith("feed/"):
            stream_id = cleaned
        else:
            stream_id = self.stream_id_for_feed_url(cleaned)
            if stream_id is None:
                # Already absent from FreshRSS — treat as done so the caller can still
                # reconcile its local copy instead of failing on an idempotent request.
                return
        with httpx.Client(timeout=30.0, follow_redirects=True) as client:
            data: dict[str, Any] = {"ac": "unsubscribe", "s": stream_id}
            try:
                token = self.token(client)
            except FreshRSSError:
                token = None
            if token:
                data["T"] = token
            self.post_form(client, "/api/greader.php/reader/api/0/subscription/edit", data)

    def subscriptions(self) -> list[dict[str, Any]]:
        with httpx.Client(timeout=30.0, follow_redirects=True) as client:
            data = self.get_json(client, "/api/greader.php/reader/api/0/subscription/list", {"output": "json"})
        return list(data.get("subscriptions") or [])

    def reading_list(self, limit: int) -> list[dict[str, Any]]:
        params = {"output": "json", "n": str(limit)}
        paths = [
            "/api/greader.php/reader/api/0/stream/contents/user/-/state/com.google/reading-list",
            "/api/greader.php/reader/api/0/stream/contents/reading-list",
        ]
        last_error: Exception | None = None
        with httpx.Client(timeout=45.0, follow_redirects=True) as client:
            for path in paths:
                try:
                    data = self.get_json(client, path, params)
                    return list(data.get("items") or [])
                except Exception as exc:  # Try FreshRSS-compatible path variants before failing.
                    last_error = exc
        if last_error:
            raise last_error
        return []


def _item_url(item: dict[str, Any]) -> str | None:
    canonical = item.get("canonical") or []
    alternate = item.get("alternate") or []
    for collection in (canonical, alternate):
        for link in collection:
            href = link.get("href") if isinstance(link, dict) else None
            if href:
                return href
    return item.get("url")


def _summary(item: dict[str, Any]) -> str | None:
    summary = item.get("summary") or item.get("content")
    if isinstance(summary, dict):
        return summary.get("content") or summary.get("direction")
    if isinstance(summary, str):
        return summary
    return None


def _published_at(item: dict[str, Any]) -> datetime | None:
    value = item.get("published") or item.get("updated")
    if isinstance(value, int | float):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    value = item.get("timestampUsec")
    if value:
        try:
            return datetime.fromtimestamp(int(value) / 1_000_000, tz=timezone.utc)
        except ValueError:
            return None
    return None


def normalize_subscription(item: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    feed_url = item.get("url") or item.get("id")
    youtube_channel_id = youtube_channel_id_from_text(feed_url) or youtube_channel_id_from_text(item.get("htmlUrl"))
    return item, youtube_channel_id


def normalize_entry(item: dict[str, Any]) -> dict[str, Any] | None:
    url = _item_url(item)
    youtube_video_id = youtube_video_id_from_url(url) or youtube_video_id_from_url(item.get("id"))
    if not youtube_video_id:
        return None

    origin = item.get("origin") or {}
    subscription_id = origin.get("streamId") or item.get("originId")
    categories = item.get("categories") or []
    feed_url = origin.get("htmlUrl") or origin.get("streamId")
    youtube_channel_id = youtube_channel_id_from_text(feed_url) or youtube_channel_id_from_text(subscription_id)

    thumbnail_url = f"https://i.ytimg.com/vi/{youtube_video_id}/hqdefault.jpg"
    return {
        "id": item.get("id") or f"youtube:{youtube_video_id}",
        "youtube_video_id": youtube_video_id,
        "youtube_channel_id": youtube_channel_id,
        "subscription_id": subscription_id,
        "channel_title": origin.get("title") or item.get("author") or "Unknown channel",
        "feed_url": feed_url,
        "title": item.get("title") or youtube_video_id,
        "url": url or f"https://www.youtube.com/watch?v={youtube_video_id}",
        "thumbnail_url": thumbnail_url,
        "author": item.get("author") or origin.get("title"),
        "summary": _summary(item),
        "published_at": _published_at(item),
        "is_read": any(READ_CATEGORY in str(category) for category in categories),
        "is_starred": any(STAR_CATEGORY in str(category) for category in categories),
        "raw": item,
    }
