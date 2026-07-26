from __future__ import annotations

import logging
from typing import Any

from . import db
from .config import get_settings
from .freshrss import FreshRSSClient, FreshRSSConfigError, normalize_entry, normalize_subscription

logger = logging.getLogger(__name__)


def sync_freshrss_once() -> dict[str, Any]:
    settings = get_settings()
    sync_id = db.create_sync_run("freshrss")
    subscriptions_count = 0
    entries_count = 0
    try:
        client = FreshRSSClient(settings)
        subscriptions = client.subscriptions()
        for subscription in subscriptions:
            normalized, youtube_channel_id = normalize_subscription(subscription)
            db.upsert_subscription(normalized, youtube_channel_id)
        subscriptions_count = len(subscriptions)

        entries = client.reading_list(settings.freshrss_reading_list_limit)
        for raw_entry in entries:
            entry = normalize_entry(raw_entry)
            if not entry:
                continue
            channel_id = db.upsert_channel(entry["channel_title"], entry.get("youtube_channel_id"), entry.get("feed_url"))
            video_id = db.upsert_video(entry, channel_id, entry["youtube_video_id"])
            db.upsert_entry(entry, video_id, entry.get("subscription_id"))
            entries_count += 1

        message = f"Imported {subscriptions_count} subscriptions and {entries_count} YouTube entries."
        db.finish_sync_run(sync_id, "success", message, subscriptions_count, entries_count)
        logger.info(message)
        return {
            "ok": True,
            "sync_run_id": sync_id,
            "subscriptions_count": subscriptions_count,
            "entries_count": entries_count,
            "message": message,
        }
    except FreshRSSConfigError as exc:
        message = str(exc)
        db.finish_sync_run(sync_id, "configuration_error", message, subscriptions_count, entries_count)
        logger.warning(message)
        return {"ok": False, "sync_run_id": sync_id, "status": "configuration_error", "message": message}
    except Exception as exc:
        message = f"FreshRSS sync failed: {exc}"
        db.finish_sync_run(sync_id, "error", message, subscriptions_count, entries_count)
        logger.exception(message)
        return {"ok": False, "sync_run_id": sync_id, "status": "error", "message": message}
