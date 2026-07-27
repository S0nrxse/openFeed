"""Offline evaluation for the recommendation ranker.

Every weight in the scorer is a hand-typed constant, and until now there was no way to
tell whether changing one helped or hurt. This holds out the most recently watched videos,
asks the ranker to produce a feed from everything *before* them, and measures how much of
the held-out set it actually surfaced.

The metrics are the standard ones for ranked retrieval:

* **recall@k** - of the videos the user really went on to watch, what fraction appeared in
  the top k. This is the headline number: it answers "would the feed have shown me this?"
* **MRR** - mean reciprocal rank of the first hit. Rewards putting a good video first
  rather than twentieth.
* **nDCG@k** - like recall but discounted by position, so a hit at rank 2 counts for more
  than a hit at rank 20.

A caveat worth stating plainly: this can only measure whether the ranker resurfaces things
the user already chose, which is biased toward what the old feed happened to show them. It
cannot measure whether a video they never saw would have been good. It is a regression
guard and a relative comparison tool, not a measure of absolute quality.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from . import db

logger = logging.getLogger("openfeed.evaluation")

DEFAULT_K_VALUES = (10, 20, 50)


@dataclass
class EvalResult:
    """One evaluation run, ready to print or serve as JSON."""

    lane: str
    holdout: int
    candidates: int
    recall: dict[int, float] = field(default_factory=dict)
    ndcg: dict[int, float] = field(default_factory=dict)
    mrr: float = 0.0
    hits: list[str] = field(default_factory=list)
    misses: list[str] = field(default_factory=list)
    note: str = ""

    def public(self) -> dict[str, Any]:
        return {
            "lane": self.lane,
            "holdout": self.holdout,
            "candidates": self.candidates,
            "recall": {f"@{k}": round(v, 4) for k, v in sorted(self.recall.items())},
            "ndcg": {f"@{k}": round(v, 4) for k, v in sorted(self.ndcg.items())},
            "mrr": round(self.mrr, 4),
            "hits": self.hits,
            "misses": self.misses[:20],
            "note": self.note,
        }


def _reciprocal_rank(ranked: list[str], relevant: set[str]) -> float:
    for index, video_id in enumerate(ranked, start=1):
        if video_id in relevant:
            return 1.0 / index
    return 0.0


def _ndcg_at(ranked: list[str], relevant: set[str], k: int) -> float:
    """Binary-relevance nDCG. Ideal DCG is the best achievable given how many exist."""
    gain = sum(1.0 / math.log2(index + 1) for index, video_id in enumerate(ranked[:k], start=1) if video_id in relevant)
    ideal_hits = min(len(relevant), k)
    if not ideal_hits:
        return 0.0
    ideal = sum(1.0 / math.log2(index + 1) for index in range(1, ideal_hits + 1))
    return gain / ideal if ideal else 0.0


def holdout_watch_history(user_key: str = "local", holdout: int = 20) -> tuple[list[str], list[str]]:
    """Split watch history into (older, most-recent-holdout), newest last.

    Splitting by time rather than at random is deliberate: a random split lets the ranker
    peek at the future, which flatters any model that leans on recency.
    """
    rows = db.list_history_videos(500, user_key)
    ordered = [
        str(row.get("youtube_video_id") or "")
        for row in reversed(rows)  # list_history_videos is newest-first
        if row.get("youtube_video_id")
    ]
    if len(ordered) <= holdout:
        return ordered, []
    return ordered[:-holdout], ordered[-holdout:]


def evaluate_lane(
    lane: str = "recommended",
    user_key: str = "local",
    holdout: int = 20,
    depth: int = 100,
    k_values: Iterable[int] = DEFAULT_K_VALUES,
    ranker: Callable[[int, int, str, str], list[dict[str, Any]]] | None = None,
) -> EvalResult:
    """Score one feed lane against held-out watch history.

    `ranker` is injectable so a candidate implementation can be measured against the live
    one without being wired into the app first.
    """
    rank = ranker or db.list_recommended_videos
    _, held = holdout_watch_history(user_key, holdout)
    result = EvalResult(lane=lane, holdout=len(held), candidates=0)
    if not held:
        result.note = "Not enough watch history to hold anything out."
        return result

    ranked_rows = rank(depth, 0, lane, user_key)
    ranked = [str(row.get("youtube_video_id") or "") for row in ranked_rows if row.get("youtube_video_id")]
    result.candidates = len(ranked)
    relevant = set(held)

    # The ranker excludes anything already watched, which is correct for a live feed and
    # fatal for an offline test: every held-out video would be filtered out by definition.
    # Videos it cannot possibly return are reported rather than silently counted as misses.
    returnable = {video_id for video_id in ranked}
    unreachable = [video_id for video_id in held if video_id not in returnable]
    if len(unreachable) == len(held):
        result.note = (
            "Every held-out video was filtered from the candidate pool, most likely because "
            "the ranker hides watched videos. Compare lanes relatively, not absolutely."
        )

    for k in k_values:
        top = ranked[:k]
        found = sum(1 for video_id in top if video_id in relevant)
        result.recall[k] = found / len(relevant)
        result.ndcg[k] = _ndcg_at(ranked, relevant, k)
    result.mrr = _reciprocal_rank(ranked, relevant)
    result.hits = [video_id for video_id in ranked if video_id in relevant][:20]
    result.misses = unreachable
    return result


def compare_lanes(
    lanes: Iterable[str] = ("recommended", "all"),
    user_key: str = "local",
    holdout: int = 20,
    depth: int = 100,
) -> dict[str, Any]:
    """Run several lanes over one split so the numbers are directly comparable."""
    results = {lane: evaluate_lane(lane, user_key, holdout, depth).public() for lane in lanes}
    ranked = sorted(results.items(), key=lambda item: item[1]["recall"].get("@20", 0), reverse=True)
    return {
        "ok": True,
        "user_key": user_key,
        "holdout": holdout,
        "depth": depth,
        "lanes": results,
        "best_by_recall_at_20": ranked[0][0] if ranked else "",
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


def full_report(user_key: str = "local", holdout: int = 20, depth: int = 100) -> dict[str, Any]:
    return {
        "ok": True,
        "quality": compare_lanes(("recommended", "all"), user_key, holdout, depth),
        "coverage": coverage_report(user_key, depth),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate the OpenFeed recommendation ranker.")
    parser.add_argument("--user", default="local")
    parser.add_argument("--holdout", type=int, default=20)
    parser.add_argument("--depth", type=int, default=100)
    parser.add_argument("--json", action="store_true", help="print raw JSON instead of a summary")
    args = parser.parse_args()

    report = full_report(args.user, args.holdout, args.depth)
    if args.json:
        print(json.dumps(report, indent=2))
        return

    quality = report["quality"]
    coverage = report["coverage"]
    print(f"holdout={quality['holdout']} depth={quality['depth']}\n")
    print(f"{'lane':<14}{'recall@10':>11}{'recall@20':>11}{'ndcg@20':>10}{'mrr':>8}")
    for lane, data in quality["lanes"].items():
        print(
            f"{lane:<14}{data['recall'].get('@10', 0):>11.3f}{data['recall'].get('@20', 0):>11.3f}"
            f"{data['ndcg'].get('@20', 0):>10.3f}{data['mrr']:>8.3f}"
        )
    note = next((d["note"] for d in quality["lanes"].values() if d.get("note")), "")
    if note:
        print(f"\nnote: {note}")

    print(f"\ncoverage over {coverage['videos']} videos")
    print(f"  distinct creators : {coverage['distinct_channels']}")
    print(f"  max per creator   : {coverage['max_per_channel']} ({coverage['top_channel_share']:.0%} share)")
    print(f"  sources           : {coverage['sources']}")
    print(f"  score range       : {coverage['score_range']}")
    print("  signal coverage   :")
    for name, share in coverage["signal_coverage"].items():
        flag = "  <- fires on everything" if share >= 0.98 else ""
        print(f"      {name:<32}{share:>6.0%}{flag}")


if __name__ == "__main__":
    main()
