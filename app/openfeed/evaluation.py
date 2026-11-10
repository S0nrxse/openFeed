"""Offline evaluation for the recommendation ranker: leave one subscribed channel out.

The previous harness held out the most recent watches and asked the ranker to resurface
them. It could not work: the real history was shorter than the holdout, and the ranker
excludes watched videos by design, so every held-out video was unreachable and every
metric was 0 whatever the ranker did.

This one asks a question the live data can answer. For each subscribed channel with at
least `min_videos` videos, drop it from the seeds, rank the whole library, and see where
that channel's videos land among everything you do not follow. A ranker that understands
your subscriptions should place a channel you chose near the top even when it was not
told about it. Reported per mode:

* **nDCG@20** - binary relevance, discounted by position.
* **recall@100** - share of the held-out channel's videos in the top 100.
* **random** - the expectation of each metric under a uniformly random order of the same
  candidates, so a number can be read against chance.

Limits, stated plainly: positives are a whole channel, so this measures "finds channels
like the ones you follow", not "finds the next video you will enjoy". The held-out
channel's own videos are linked to each other in YouTube's related lists, and the walk
does not use that (the channel is not a seed). Classic cannot be re-seeded (it reads the
subscriptions from the database), so its row is computed from the live feed with the
held-out channel still known to it, which favours classic.
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from typing import Any

from . import db, ranker

logger = logging.getLogger("openfeed.evaluation")


def _subscribed_keys(user_key: str) -> list[str]:
    """The channels this user follows, lowercased."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT lower(btrim(f.youtube_channel_id)) AS k FROM feeds f "
            "JOIN user_subscriptions us ON us.feed_id = f.id "
            "WHERE us.user_key = %s AND f.youtube_channel_id IS NOT NULL AND btrim(f.youtube_channel_id) <> ''",
            (user_key,),
        ).fetchall()
    return [str(row["k"]) for row in rows]


def _classic_ranked_ids(user_key: str, depth: int = 200) -> list[int]:
    """The legacy recommendation lane (it returns at most 200 rows)."""
    with ranker.forced_mode("classic"):
        rows = db.list_recommended_videos(min(depth, 200), 0, "recommended", user_key)
    return [int(row["id"]) for row in rows if row.get("id") is not None]


def leave_one_channel_out(
    user_key: str = "local",
    min_videos: int = 5,
    max_channels: int = 0,
    include_classic: bool = True,
    index: Any = None,
    subscribed: list[str] | None = None,
) -> dict[str, Any]:
    """Run the evaluation on the live library. `index`/`subscribed` are injectable for tests."""
    if not ranker.NUMPY_AVAILABLE:
        return {"ok": False, "note": "numpy is not installed, so ranker v2 cannot be evaluated."}
    started = time.perf_counter()
    if index is None:
        index = ranker.INDEX.current()
        if index is None:
            # The first build reads every video's metadata (~40 s); it runs in the
            # background rather than inside this request.
            return {"ok": False, "note": f"The ranking index is not built yet ({ranker.INDEX.state}); try again shortly."}
    keys = subscribed if subscribed is not None else _subscribed_keys(user_key)
    classic_ids: list[int] | None = None
    note = ""
    if include_classic:
        try:
            classic_ids = _classic_ranked_ids(user_key)
        except Exception as exc:  # noqa: BLE001 - classic is optional here
            note = f"classic not computed: {exc}"
    result = ranker.loco_evaluate(index, keys, ("graph", "blend"), min_videos, max_channels, classic_ids)
    return {
        "ok": True,
        "method": "leave-one-subscribed-channel-out",
        "min_videos": min_videos,
        "index_version": index.version,
        "index_videos": index.n,
        "subscribed_channels": len(keys),
        "default_mode": ranker.DEFAULT_MODE,
        "seconds": round(time.perf_counter() - started, 2),
        "note": note or (
            "classic is the live legacy lane (top 200), not re-seeded per channel; its numbers are favoured by that leak."
            if classic_ids is not None else ""
        ),
        **result,
    }


def coverage_report(user_key: str = "local", depth: int = 100) -> dict[str, Any]:
    """What the feed is actually made of, and which signals fired.

    Recall says nothing about variety, and a ranker can score well while showing the same
    four creators. This is the counterweight.
    """
    from collections import Counter

    rows = db.list_recommended_videos(depth, 0, "recommended", user_key)
    channels = Counter(str(row.get("channel_title") or row.get("author") or "?") for row in rows)
    sources = Counter(str(row.get("source_kind") or "?") for row in rows)
    signals: Counter[str] = Counter()
    for row in rows:
        for key in (row.get("score_debug") or {}):
            if key != "base":
                signals[key] += 1
    scores = [float(row.get("recommendation_score") or 0) for row in rows]
    top_share = (channels.most_common(1)[0][1] / len(rows)) if rows and channels else 0.0
    return {
        "ok": True,
        "videos": len(rows),
        "distinct_channels": len(channels),
        "max_per_channel": max(channels.values()) if channels else 0,
        "top_channel_share": round(top_share, 3),
        "sources": dict(sources),
        # A signal that fires on everything is not discriminating between anything.
        "signal_coverage": {name: round(count / len(rows), 3) for name, count in signals.most_common()} if rows else {},
        "score_range": [round(min(scores), 2), round(max(scores), 2)] if scores else [0, 0],
    }


def full_report(
    user_key: str = "local",
    min_videos: int = 5,
    max_channels: int = 0,
    include_classic: bool = True,
    depth: int = 100,
) -> dict[str, Any]:
    return {
        "ok": True,
        "quality": leave_one_channel_out(user_key, min_videos, max_channels, include_classic),
        "coverage": coverage_report(user_key, depth),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate the OpenFeed recommendation ranker (leave one channel out).")
    parser.add_argument("--user", default="local")
    parser.add_argument("--min-videos", type=int, default=5)
    parser.add_argument("--max-channels", type=int, default=0)
    parser.add_argument("--no-classic", action="store_true")
    parser.add_argument("--json", action="store_true", help="print raw JSON instead of a summary")
    args = parser.parse_args()

    # A one-off process has no warm index (the app builds it on a thread at startup), so
    # build it here; with the on-disk related cache this takes seconds, not a full read.
    if ranker.NUMPY_AVAILABLE and ranker.INDEX.current() is None:
        ranker.INDEX.rebuild()
    quality = leave_one_channel_out(args.user, args.min_videos, args.max_channels, not args.no_classic)
    if args.json:
        print(json.dumps(quality, indent=2, default=str))
        return
    if not quality.get("ok"):
        print(quality.get("note"))
        return
    print(f"{quality['units']} channels held out ({quality['skipped_small']} too small), "
          f"index {quality['index_videos']} videos, {quality['seconds']}s\n")
    print(f"{'mode':<9}{'nDCG@20':>9}{'random':>9}{'recall@100':>12}{'random':>9}{'hit units':>11}")
    for mode, row in quality["summary"].items():
        print(f"{mode:<9}{row['ndcg@20']:>9.4f}{row['random_ndcg@20']:>9.4f}{row['recall@100']:>12.4f}"
              f"{row['random_recall@100']:>9.4f}{row['units_with_hit_in_top20']:>11}")
    if quality.get("note"):
        print(f"\nnote: {quality['note']}")


if __name__ == "__main__":
    main()
