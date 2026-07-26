from __future__ import annotations

import logging
import time

from . import db
from .config import get_settings
from .discovery import discover_related_videos
from .enrichment import enrich_channels, enrich_recent_videos
from .sync import sync_freshrss_once

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("openfeed.worker")


def main() -> None:
    db.init_db()
    settings = get_settings()
    interval = max(1200, settings.sync_interval_seconds)
    logger.info("worker started with interval=%s seconds", interval)
    while True:
        settings = get_settings()
        if settings.freshrss_configured:
            sync_freshrss_once()
            if settings.enrich_after_sync_limit > 0:
                result = enrich_recent_videos(settings.enrich_after_sync_limit)
                logger.info("metadata enrichment batch: %s", result)
            channel_result = enrich_channels(10)
            if channel_result.get("candidates_count"):
                logger.info("channel avatar/banner batch: %s", channel_result)
            if settings.discover_after_sync_limit > 0:
                result = discover_related_videos(settings.discover_after_sync_limit, settings.discover_related_per_video)
                logger.info("invidious discovery batch: %s", result)
        else:
            logger.warning("FreshRSS credentials are not configured; skipping sync")
        time.sleep(interval)


if __name__ == "__main__":
    main()
