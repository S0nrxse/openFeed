"""One-off repair for publish dates fabricated by Invidious list endpoints.

Those endpoints carry YouTube's relative text ("2 days ago") and decode it to an absolute
time at request time, so a missing or unrecognised string decodes to *now*. Every video
acquired while that went unchecked was dated to the moment it was fetched, which made old
episodes the newest thing on their channel and handed them the feed's recency boost.

The single-video endpoint does report a real timestamp, so this asks it rather than
guessing: for each suspect row it fetches the truth and writes it back. Rows it cannot
reach are left alone and counted, never overwritten with an assumption.

Run inside the app container:
    python -m openfeed.tools.repair_publish_dates [--commit] [--limit N]
"""

from __future__ import annotations

import argparse
import datetime
import sys
import time

import httpx

from .. import db
from ..config import get_settings

# Anything dated inside this window is suspect: fabrication always lands at "now", and a
# genuinely recent upload will simply be confirmed by the lookup rather than changed.
SUSPECT_WINDOW = "14 days"
# Invidious proxies each of these to YouTube, so a fast loop rate-limits it and it starts
# answering 500 "Can't load the video on this instance". That failure is indistinguishable
# from a genuinely unavailable video, and at 0.15s it produced 820 false unreachables in
# one run — videos that answered fine on a slower retry. Pace it like a person browsing.
PACE_SECONDS = 1.0
RETRY_BACKOFF_SECONDS = 8.0


def candidates(limit: int) -> list[dict]:
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT youtube_video_id, title, published_at, view_count
                FROM videos
                WHERE published_at IS NULL
                   OR published_at > now() - interval '{SUSPECT_WINDOW}'
                   OR published_at > now()
                   OR published_at BETWEEN created_at - interval '5 min' AND updated_at + interval '5 min'
                ORDER BY published_at DESC NULLS LAST
                LIMIT %s
                """,
                (limit,),
            )
            return list(cur.fetchall())


def truth(client: httpx.Client, base: str, video_id: str) -> tuple[datetime.datetime | None, int | None]:
    """The real publish time, or None if the lookup genuinely could not answer.

    A 500 here usually means we asked too fast rather than that the video is gone, so it
    gets one slow retry before being counted as unreachable. Without that, throttling
    looks exactly like a deleted video and the run reports hundreds of false failures.
    """
    response = client.get(f"{base}/api/v1/videos/{video_id}")
    if response.status_code >= 500:
        time.sleep(RETRY_BACKOFF_SECONDS)
        response = client.get(f"{base}/api/v1/videos/{video_id}")
    if response.status_code != 200:
        return None, None
    payload = response.json()
    raw = payload.get("published")
    published = datetime.datetime.fromtimestamp(raw, datetime.timezone.utc) if raw else None
    return published, payload.get("viewCount") or None


def apply(video_id: str, published: datetime.datetime, views: int | None) -> None:
    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE videos
                SET published_at = %s,
                    view_count = COALESCE(%s, view_count),
                    updated_at = now()
                WHERE youtube_video_id = %s
                """,
                (published, views, video_id),
            )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", action="store_true", help="write the corrections (otherwise report only)")
    parser.add_argument("--limit", type=int, default=2000)
    args = parser.parse_args()

    base = get_settings().invidious_base_url
    rows = candidates(args.limit)
    print(f"{len(rows)} suspect rows; {'WRITING' if args.commit else 'dry run'}", flush=True)

    fixed = confirmed = unreachable = 0
    with httpx.Client(timeout=20, follow_redirects=True) as client:
        for index, row in enumerate(rows, 1):
            video_id = row["youtube_video_id"]
            try:
                published, views = truth(client, base, video_id)
            except Exception:
                unreachable += 1
                continue
            if published is None:
                unreachable += 1
                continue
            stored = row["published_at"]
            drift = abs((stored - published).total_seconds()) / 86400 if stored else None
            if drift is not None and drift <= 1.0:
                confirmed += 1
            else:
                fixed += 1
                where = f"{stored.date()} -> {published.date()}" if stored else f"(undated) -> {published.date()}"
                print(f"  {where}  {row['title'][:52]}", flush=True)
                if args.commit:
                    apply(video_id, published, views)
            if index % 100 == 0:
                print(f"  ... {index}/{len(rows)}", flush=True)
            time.sleep(PACE_SECONDS)

    print(f"\ncorrected {fixed} | already right {confirmed} | unreachable {unreachable}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
