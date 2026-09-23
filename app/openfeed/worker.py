from __future__ import annotations

import logging
import time

from . import db, ryd, topics
from .config import get_settings
from .discovery import backfill_subscription_uploads, discover_related_videos, grow_library
from .enrichment import (
    backfill_subscription_art,
    enrich_channels,
    enrich_channels_paced,
    enrich_recent_videos,
    verify_publish_dates,
)
from .rss import sync_rss_once

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("openfeed.worker")


def run_once() -> None:
    """One sync/enrich/discover cycle. Extracted for testability."""
    settings = get_settings()
    try:
        sync_rss_once(settings.rss_poll_limit)
        if settings.enrich_after_sync_limit > 0:
            result = enrich_recent_videos(settings.enrich_after_sync_limit)
            logger.info("metadata enrichment batch: %s", result)
    except Exception as exc:  # noqa: BLE001 - one RSS failure can't kill the loop
        logger.warning("sync/enrich cycle skipped: %s", exc)
    else:
        # Dates decoded from "2 days ago" are wrong by up to a year, and the video
        # they belong to keeps outranking everything because the feed rewards recency.
        # A bounded batch per cycle drains the backlog without hammering Invidious.
        try:
            dates = verify_publish_dates()
            if dates.get("corrected"):
                logger.info("publish dates re-read: %s", dates)
        except Exception as exc:  # noqa: BLE001
            logger.info("publish date pass skipped: %s", exc)
        # Subscribed channels first and to completion: they are the ones with a blank
        # circle sitting in the sidebar, and there are ten discovered channels for
        # every one of them, so a shared queue never reaches them.
        try:
            art = backfill_subscription_art()
            if art.get("updated"):
                logger.info("subscription art: %s", art)
        except Exception as exc:  # noqa: BLE001
            logger.info("subscription art skipped: %s", exc)
        # Paced: a bulk subscribe or a Takeout import can leave dozens of channels
        # with no art at once, and fetching them all immediately is a burst.
        try:
            channel_result = enrich_channels_paced()
            if channel_result.get("updated"):
                logger.info("channel art batch: %s", channel_result)
        except Exception as exc:  # noqa: BLE001
            logger.info("channel art batch skipped: %s", exc)
        # Channel RSS feeds only carry the most recent handful per channel, and only
        # from the moment you subscribed, so the date-ordered feed is full of holes
        # without this.
        try:
            backfill = backfill_subscription_uploads(200)
            if backfill.get("stored"):
                logger.info("subscription backfill: %s", backfill)
        except Exception as exc:  # noqa: BLE001
            logger.info("subscription backfill skipped: %s", exc)

        if settings.discover_after_sync_limit > 0:
            # Every acquisition source, not just related-video crawling. Related
            # only ever returns neighbours of what is already stored, so on its own
            # the library narrows toward whatever it started with.
            try:
                result = grow_library("local")
                logger.info("library growth: stored=%s", result.get("stored_total"))
            except Exception as exc:  # noqa: BLE001
                logger.info("library growth skipped: %s", exc)

    # Topic vectors follow enrichment: a video with no description yet would only
    # produce a vector of its title, and would then never be revisited.
    try:
        result = topics.rebuild_vectors(400)
        if result.get("updated"):
            logger.info("topic vector batch: %s", result)
    except Exception as exc:  # noqa: BLE001
        logger.info("topic vector batch skipped: %s", exc)

    # Dislike estimates are filled in here, in the background, deliberately: doing it
    # when somebody opens a video would let RYD time its requests against real views.
    # refresh_missing is a no-op unless the user has opted in.
    try:
        result = ryd.refresh_missing(settings.ryd_batch_size)
        if result.get("updated"):
            logger.info("dislike estimate batch: %s", result)
    except Exception as exc:  # noqa: BLE001 - a third party being down is not fatal
        logger.info("dislike estimate batch skipped: %s", exc)


def main() -> None:
    db.init_db()
    settings = get_settings()
    interval = max(1200, settings.sync_interval_seconds)
    logger.info("worker started with interval=%s seconds", interval)
    while True:
        run_once()
        # The sleep is the backoff: a failed cycle logs and retries next interval
        # instead of spinning or exiting.
        time.sleep(interval)


if __name__ == "__main__":
    main()
