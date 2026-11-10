"""Recommendation ranker v2: related-video graph retrieval over the whole library.

The legacy ranker (db.list_recommended_videos' SQL plus _apply_recommendation_scores)
scored a 2,000-row pool chosen by recency, so 93% of the library could never be
recommended, and it ignored the one collaborative signal OpenFeed has: the related-video
lists Invidious stores for each enriched video (metadata.recommendedVideos).

v2 in plain terms: "videos that YouTube's own related lists connect to the channels you
follow, where a related id shared by many videos counts for less". Each stored related id
is a *context*. Every subscribed channel puts mass 1 on its videos (spread evenly), every
graded positive event (a real watch, a like, more-like-this) adds its own mass, and the
mass walks seed -> context -> video twice (the second hop at half weight). Seeds score 0.
Videos the walk never reaches are ordered by a velocity prior,
log1p(views) - 1.5 * log(age_days + 1).

Modes (one constant decides the default; users can override it in Settings):
  graph    RC1: graph score first, velocity orders the unreached remainder
  blend    RC2: 0.25 * minmax(graph) + 0.75 * minmax(velocity) over the eligible pool
  classic  the legacy ranker, untouched

Evidence and limits: the experiments behind every constant here are in the algorithm
work's scratch reports (track A: graph + fusion, track B: event grading and the MMR page);
the docs (README "Ranking") link them. Offline, the graph was a null result on the
OpenFeed testbed (23 units, underpowered) and graph+prior was +9.2% nDCG@20 on the
KuaiRec testbed (p<1e-5, below the pre-registered +10%). Nothing here is tuned on live data.

Everything is numpy only. The library index is built in a background thread and served
from process memory; until it exists, or whenever numpy is missing or anything raises,
callers fall back to the legacy ranker and the reason is logged once.
"""

from __future__ import annotations

import contextlib
import contextvars
import dataclasses
import logging
import math
import queue
import random
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator

try:  # numpy is a hard requirement of v2 only; the legacy ranker runs without it.
    import numpy as np
except Exception:  # pragma: no cover - exercised by patching NUMPY_AVAILABLE in tests
    np = None  # type: ignore[assignment]

logger = logging.getLogger("openfeed.ranker")

NUMPY_AVAILABLE = np is not None

# ---- the default ------------------------------------------------------------------------
# The ONE place the shipped default lives. A sealed verification decides between graph and
# blend (prereg 001 D16); flip this constant, nothing else.
DEFAULT_MODE = "graph"
MODES = ("graph", "blend", "classic")
MODE_LABELS = {
    "graph": "Graph",
    "blend": "Graph + popularity",
    "classic": "Classic",
}
MODE_NOTES = {
    "graph": "Videos linked from the related lists of channels you follow, then popular and recent ones.",
    "blend": "A quarter related-list links, three quarters popular-and-recent, across the whole library.",
    "classic": "The original topic and watch-time scorer over the newest 2,000 videos.",
}
V2_LANES = ("recommended", "all")
# Every lane recommended_rows serves: the two shelves, the deeper Home lanes and the Shorts shelf.
SERVED_LANES = (*V2_LANES, "recommended-recent", "recommended-long", "shorts")

# ---- parameters (exp/A/ref_A.PARAMS and exp/B/ref_B; chosen on DEV, never on live data) --
HUB = "prob"                 # context weight 1/(videos listing that id): 5/5 T1 folds
ROUNDS = 2                   # seed -> context -> item, twice
SECOND_HOP_WEIGHT = 0.5
BETA = 0.0                   # item-degree discount; 0/.3/.6 indistinguishable on T1 and T2
PRIOR_GAMMA = 1.5
PRIOR_TAU_DAYS = 1.0
UNDATED_AGE_DAYS = 3650.0    # an undated video is treated as ten years old by the prior
W_GRAPH, W_PRIOR = 0.25, 0.75
EVENT_SEED_WEIGHT = 1.0      # relative weight of a high-graded event within the event part
PERSONAL_K = 20.0            # events get n/(n+K) of the seed mass (track B, H4)
SHORT_MAX_SECONDS = 90

# Event grading (ref_B.build_events)
WEAK_RATIO = 0.5
HIGH_RATIO = 0.9
W_MID, W_HIGH, W_EXPLICIT = 0.5, 1.0, 1.0

# Negatives: the exact video is always excluded; a channel is demoted only on evidence.
CHANNEL_MIN_NEGATIVES = 2
CHANNEL_DEMOTION = 0.5

# Page step (D16 page policy): MMR on topic cosine with a per-channel factor.
MMR_LAMBDA = 0.7
MMR_TOP = 200
MMR_SLOTS = 50
MMR_CHANNEL_DECAY = 0.8

# Feel (docs/ranker-v2/feel-preregistration.md). The sealed test was age-matched, so it could
# not see freshness; these are product calibration, chosen by the rule written there.
FEEL = {
    "half_life_days": 21.0,  # freshness F = max(floor, 2^(-age/H)), applied as g * F^beta
    "floor": 0.25,
    "beta": 0.0,             # freshness multiplier OFF: it cut LOCO relevance and barely moved age (Amendment 1)
    "gamma": 1.0,            # quality Q = 0.5 + 0.5 * velocity percentile, applied as g * Q^gamma
}
# Graph mode is a slot mix of three candidate sources (Amendment 1):
#   watched  walk from the user's graded events         share n/(n + PERSONAL_SLOT_K)
#   fresh    new uploads from creators the walks link   FRESH_SLOT_SHARE of the rest
#   channels walk from subscriptions, then the prior    the remainder
PERSONAL_SLOT_K = 20.0          # judges: K=20 preferred over K=5 20/20 (feel-preregistration.md, results)
FRESH_SLOT_SHARE = 0.3       # Amendment 2: "new from related creators", 30% of the non-watched slots
FRESH_MAX_DAYS = 30.0        # uploads up to this old, weighted 2^(-age/FRESH_MAX_DAYS)
CREATOR_TOP_VIDEOS = 3       # creator affinity = sum of the channel's top-3 walk scores
SOURCES = ("watched", "fresh", "channels")
DEAD_MAX_VIEWS = 100         # known views below this ...
DEAD_MIN_AGE_DAYS = 7.0      # ... on a video older than this: not a pick
ROTATION_DECAY = 0.7         # per earlier day shown (last 7 days); 0.75 kept 75% of the top 24, 0.7 keeps 58% (Amendment 2)
ROTATION_MAX_DAYS = 3
ROTATION_WINDOW_DAYS = 7      # only days before today count, so one day's pages stay consistent
WATCHED_SHARE_MAX = 0.5       # audit: n/(n+K) reached 74% at 57 events; the judges preferred less
EVENT_HALF_LIFE_DAYS = 60.0   # older watches count less (weight and share); product choice, T2 null
EVENT_DECAY_FLOOR = 0.25

# Feed controls in graph mode (README "Ranking", "Feed controls"). Product calibration, not
# measured offline: the defaults (discovery 0.35, subscriptions prioritised) reproduce the
# measured mix exactly, and the extremes are reported with LOCO in docs/ranker-v2/README.md.
DISCOVERY_DEFAULT = 0.35
# Bounds chosen so each control alone stays within 10% of the default's plain LOCO (README):
FRESH_SHARE_MIN, FRESH_SHARE_MAX = 0.15, 0.4    # "how far to wander" 0 -> 0.15, default -> 0.3, 1 -> 0.4
WANDER_SHARE_MAX = 0.05      # unreached "popular and recent" slots at discovery 1.0; none at or below the default
NO_PRIORITY_CHANNEL_FACTOR = 0.8   # channels-source share multiplier when subscriptions are not prioritised
PREFERRED_TOPIC_BOOST = 1.25  # mild: reorders within a source, never moves a video between sources
# Imported Google Takeout history: weak positives for the watched walk. Each imported watch
# weighs TAKEOUT_WEIGHT * max(floor, 2^(-age/half-life)) <= 0.1, below the weakest in-app watch
# (W_MID * EVENT_DECAY_FLOOR = 0.125), and the whole import counts as at most five full watches.
TAKEOUT_WEIGHT = 0.1
TAKEOUT_HALF_LIFE_DAYS = 180.0
TAKEOUT_DECAY_FLOOR = 0.1
TAKEOUT_MAX_MASS = 5.0
TAKEOUT_MAX_SEEDS = 500       # most recent imported watches that seed; every imported watch is excluded
# Deeper Home lanes and the Shorts shelf (v2 lanes with a filter).
RECENT_MAX_DAYS = 14.0
LONG_FORM_MIN_SECONDS = 1200
FILTERED_LANES = ("recommended-recent", "recommended-long")
# The order Home reads these lanes in (main._home_page_lane, HOME_PAGE_SIZE = 30): pages 0-1 take
# recommended ranks 0-89, then every four pages read recent, long-form, recommended, recommended.
# The lanes draw on one pool, so each is served without the rows an earlier page took.
HOME_SCHEDULE_HEAD = (("recommended", 90),)
HOME_SCHEDULE_CYCLE = (("recommended-recent", 30), ("recommended-long", 30), ("recommended", 60))
# Watch-page Up next: the anchor's own related list first, then its walk, the user's walk as a prior.
UPNEXT_DIRECT_BONUS = 1.0     # a related-list entry's position weight, 1.0 (first) -> 0.5 (last)
UPNEXT_USER_WEIGHT = 0.05     # the user's own walk only breaks near-ties
UPNEXT_CREATOR_SLOTS = (2, 5)  # 0-based slots for the anchor creator's latest uploads
UPNEXT_CHANNEL_CAP = 2        # per other channel; the anchor's channel keeps YouTube's own follow-ups
UPNEXT_ANCHOR_CHANNEL_CAP = 5
# "Help tune recommendations": team-draft interleaving of graph against blend (H9).
INTERLEAVE_TEAMS = ("graph", "blend")
INTERLEAVE_EXPERIMENT = "tdi-graph-vs-blend-1"
INTERLEAVE_CLICK_WINDOW_HOURS = 24

RANK_DEPTH = 3000            # ranked ids kept per lane; 100 pages of infinite scroll
INDEX_MAX_AGE_SECONDS = 900.0
INDEX_CHECK_INTERVAL_SECONDS = 60.0
USER_CACHE_SIZE = 16

# A per-call override, used by the evaluation harness to rank with a specific mode
# (including "classic") without touching anybody's stored preference.
_MODE_OVERRIDE: contextvars.ContextVar[str] = contextvars.ContextVar("openfeed_ranker_mode", default="")


@contextlib.contextmanager
def forced_mode(mode: str) -> Iterator[None]:
    token = _MODE_OVERRIDE.set(mode)
    try:
        yield
    finally:
        _MODE_OVERRIDE.reset(token)


def resolve_mode(preferred: Any = "") -> str:
    """The mode a user actually gets: their choice when valid, else DEFAULT_MODE."""
    override = _MODE_OVERRIDE.get()
    if override in MODES:
        return override
    value = str(preferred or "").strip().lower()
    return value if value in MODES else DEFAULT_MODE


# ---- fallback bookkeeping -----------------------------------------------------------------
_FALLBACK_LOCK = threading.Lock()
_FALLBACK_LOGGED: set[str] = set()
_LAST_FALLBACK = {"reason": "", "at": 0.0}


def note_fallback(reason: str) -> None:
    """Record why the legacy ranker served a request; log each distinct reason once."""
    with _FALLBACK_LOCK:
        _LAST_FALLBACK["reason"] = reason
        _LAST_FALLBACK["at"] = time.time()
        if reason in _FALLBACK_LOGGED:
            return
        _FALLBACK_LOGGED.add(reason)
    logger.warning("ranker v2 unavailable, serving the classic ranker: %s", reason)


# ---- corpus index ---------------------------------------------------------------------------
@dataclass
class CorpusIndex:
    """The library as arrays. Row i is the i-th video by ascending videos.id."""

    version: int
    built_at: float
    ids: Any                      # int64[n], ascending
    chan: Any                     # int32[n] channel-key code
    chan_keys: list[str]          # code -> lowercased channel key (legacy _recommendation_channel_key)
    chan_lookup: dict[str, int]
    chan_order: Any               # rows grouped by channel code
    chan_start: Any               # offsets into chan_order, len = n_channels + 1
    r: Any                        # int32 edge rows (item), deduplicated, sorted by (r, c)
    c: Any                        # int32 edge columns (context)
    n_ctx: int
    ctx_n: Any                    # float64[n_ctx] videos listing each context
    deg: Any                      # float64[n] contexts per video
    self_ctx: Any                 # int64[n] the context column of each video's own id
    has_self: Any                 # bool[n] video already lists its own id
    row_ptr: Any                  # CSR offsets over (r, c)
    ctx_ptr: Any                  # CSC offsets
    ctx_rows: Any                 # rows per context, CSC order
    self_owner: Any               # int32[n_ctx] row whose own id is that context, or -1
    prior: Any                    # float64[n] velocity prior
    live: Any
    short: Any
    blocked: Any                  # live or Short: never on a recommendation lane
    video_count: int = 0
    max_id: int = 0
    build_seconds: float = 0.0
    related_videos: int = 0
    pub_ts: Any = None            # float64[n] publish time (epoch s), NaN if undated
    views: Any = None             # float64[n], NaN if unknown
    prior_pct: Any = None         # float64[n] percentile of the velocity prior, 0..1
    dead: Any = None              # bool[n] known views < DEAD_MAX_VIEWS and older than DEAD_MIN_AGE_DAYS
    dur: Any = None               # float64[n] duration seconds, NaN if unknown
    rel_ptr: Any = None           # int64[n+1] offsets into rel_ctx: each video's related list in YouTube's order
    rel_ctx: Any = None           # int32 context per related entry, original order (duplicates kept)

    @property
    def n(self) -> int:
        return int(len(self.ids))

    def rows_for_ids(self, video_ids: Iterable[int]) -> Any:
        want = np.asarray([int(v) for v in video_ids], dtype=np.int64)
        if not len(want):
            return np.zeros(0, dtype=np.int64)
        pos = np.searchsorted(self.ids, want)
        pos = np.clip(pos, 0, max(self.n - 1, 0))
        ok = self.ids[pos] == want if self.n else np.zeros(len(want), bool)
        return pos[ok]

    def channel_rows(self, key: str) -> Any:
        code = self.chan_lookup.get(key)
        if code is None:
            return np.zeros(0, dtype=np.int64)
        return self.chan_order[self.chan_start[code]:self.chan_start[code + 1]]

    def summary(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "videos": self.n,
            "contexts": int(self.n_ctx),
            "links": int(len(self.r)),
            "videos_with_related": int(self.related_videos),
            "channels": len(self.chan_keys),
            "built_at": self.built_at,
            "age_seconds": max(0.0, time.time() - self.built_at),
            "build_seconds": round(self.build_seconds, 2),
            "live_or_shorts": int(self.blocked.sum()),
        }


def velocity_prior(view_count: Any, published_ts: Any, now_ts: float) -> Any:
    views = np.nan_to_num(np.asarray(view_count, dtype=np.float64), nan=0.0)
    pub = np.asarray(published_ts, dtype=np.float64)
    age = np.where(np.isnan(pub), UNDATED_AGE_DAYS, (now_ts - pub) / 86400.0)
    return np.log1p(np.maximum(views, 0.0)) - PRIOR_GAMMA * np.log(np.maximum(age, 0.0) + PRIOR_TAU_DAYS)


def build_index(
    ids: Any,
    self_key: Any,
    rel_rows: Any,
    rel_keys: Any,
    channel_keys: list[str],
    view_count: Any,
    published_ts: Any,
    duration_seconds: Any,
    live_flag: Any,
    short_url: Any,
    now_ts: float,
    version: int = 1,
) -> CorpusIndex:
    """Arrays in, index out. `self_key`/`rel_keys` are int64 keys of YouTube ids, so a related
    id and a video's own id meet in the same context exactly when the YouTube ids match."""
    started = time.perf_counter()
    ids = np.asarray(ids, dtype=np.int64)
    n = len(ids)
    if n and np.any(np.diff(ids) <= 0):
        raise ValueError("ids must be strictly ascending")
    self_key = np.asarray(self_key, dtype=np.int64)
    rel_rows = np.asarray(rel_rows, dtype=np.int64)
    rel_keys = np.asarray(rel_keys, dtype=np.int64)
    keys, inverse = np.unique(np.concatenate([rel_keys, self_key]), return_inverse=True)
    n_ctx = max(int(len(keys)), 1)
    rel_ctx = inverse[: len(rel_keys)].astype(np.int64)
    self_ctx = inverse[len(rel_keys):].astype(np.int64)
    # A video listing the same related id twice is one link (ref_A: A.data[:] = 1).
    packed = np.unique(rel_rows * n_ctx + rel_ctx)
    r = (packed // n_ctx).astype(np.int32)
    c = (packed % n_ctx).astype(np.int32)
    ctx_n = np.bincount(c, minlength=n_ctx).astype(np.float64)
    deg = np.bincount(r, minlength=n).astype(np.float64)
    has_self = np.zeros(n, dtype=bool)
    if len(r):
        has_self[r[c == self_ctx[r]]] = True
    row_ptr = np.searchsorted(r, np.arange(n + 1))
    ctx_order = np.argsort(c, kind="stable")
    ctx_rows = r[ctx_order]
    ctx_ptr = np.searchsorted(c[ctx_order], np.arange(n_ctx + 1))
    self_owner = np.full(n_ctx, -1, dtype=np.int32)
    self_owner[self_ctx] = np.arange(n, dtype=np.int32)

    lowered = [str(k or "").strip().lower() for k in channel_keys]
    chan_keys = sorted(set(lowered))
    chan_lookup = {k: i for i, k in enumerate(chan_keys)}
    chan = np.fromiter((chan_lookup[k] for k in lowered), dtype=np.int32, count=n)
    chan_order = np.argsort(chan, kind="stable")
    chan_start = np.searchsorted(chan[chan_order], np.arange(len(chan_keys) + 1))

    dur = np.asarray(duration_seconds, dtype=np.float64)       # NaN = unknown
    live = np.asarray(live_flag, dtype=bool) | (dur == 0)
    short = np.asarray(short_url, dtype=bool) | ((dur > 0) & (dur <= SHORT_MAX_SECONDS))
    prior = velocity_prior(view_count, published_ts, now_ts)
    views = np.asarray(view_count, dtype=np.float64)
    pub_ts = np.asarray(published_ts, dtype=np.float64)
    order = np.argsort(prior, kind="stable")
    prior_pct = np.empty(n, dtype=np.float64)
    prior_pct[order] = (np.arange(n) + 0.5) / max(n, 1)
    age_days = (now_ts - pub_ts) / 86400.0
    dead = (~np.isnan(views)) & (views < DEAD_MAX_VIEWS) & (~np.isnan(age_days)) & (age_days > DEAD_MIN_AGE_DAYS)
    # Each video's related list in its stored order (Up next reads YouTube's own order).
    rel_order = np.argsort(rel_rows, kind="stable")
    rel_ptr = np.searchsorted(rel_rows[rel_order], np.arange(n + 1))
    index = CorpusIndex(
        version=version, built_at=time.time(), ids=ids, chan=chan, chan_keys=chan_keys,
        chan_lookup=chan_lookup, chan_order=chan_order, chan_start=chan_start, r=r, c=c,
        n_ctx=n_ctx, ctx_n=ctx_n, deg=deg, self_ctx=self_ctx, has_self=has_self,
        row_ptr=row_ptr, ctx_ptr=ctx_ptr, ctx_rows=ctx_rows, self_owner=self_owner,
        prior=prior, live=live, short=short, blocked=live | short,
        video_count=n, max_id=int(ids[-1]) if n else 0,
        related_videos=int(len(np.unique(rel_rows))) if len(rel_rows) else 0,
        pub_ts=pub_ts, views=views, prior_pct=prior_pct, dead=dead,
        dur=dur, rel_ptr=rel_ptr, rel_ctx=rel_ctx[rel_order].astype(np.int32),
    )
    index.build_seconds = time.perf_counter() - started
    return index


def index_from_records(records: list[dict[str, Any]], now_ts: float, version: int = 1) -> CorpusIndex:
    """Build from ref_A-style dicts (id, youtube_video_id, channel_id, view_count, published_ts,
    duration_seconds, live, related). Used by tests and the evaluation harness; production
    builds from SQL, where the id keys come from md5 in Postgres."""
    records = sorted(records, key=lambda v: int(v["id"]))
    vocab: dict[Any, int] = {}

    def key(value: Any) -> int:
        return vocab.setdefault(value, len(vocab))

    rel_rows: list[int] = []
    rel_keys: list[int] = []
    for i, v in enumerate(records):
        for y in v.get("related") or ():
            rel_rows.append(i)
            rel_keys.append(key(y))
    self_key = [key(v["youtube_video_id"]) for v in records]

    def num(value: Any) -> float:
        return float("nan") if value is None else float(value)

    return build_index(
        ids=[int(v["id"]) for v in records],
        self_key=self_key,
        rel_rows=rel_rows,
        rel_keys=rel_keys,
        channel_keys=[str(v.get("channel_id") or "") for v in records],
        view_count=[num(v.get("view_count")) for v in records],
        published_ts=[num(v.get("published_ts")) for v in records],
        duration_seconds=[num(v.get("duration_seconds")) for v in records],
        live_flag=[bool(v.get("live")) for v in records],
        short_url=[bool(v.get("short_url")) for v in records],
        now_ts=now_ts,
        version=version,
    )


# ---- graph walk (port of ref_A.UserScorer.graph) ------------------------------------------
@dataclass(frozen=True)
class ScopedGraph:
    """The related-video graph restricted to the rows one person may be shown
    (catalog_scope.py): edges, video degrees and context sizes over those rows only, so a
    video outside the scope neither appears nor bridges two videos inside it."""

    r: Any
    c: Any
    deg: Any
    ctx_n: Any


def scoped_graph(index: CorpusIndex, mask: Any) -> ScopedGraph:
    keep = mask[index.r] if len(index.r) else np.zeros(0, dtype=bool)
    r, c = index.r[keep], index.c[keep]
    return ScopedGraph(r=r, c=c, deg=np.bincount(r, minlength=index.n).astype(np.float64),
                       ctx_n=np.bincount(c, minlength=index.n_ctx).astype(np.float64))


def related_rows(index: CorpusIndex, rows_mask: Any) -> Any:
    """Rows whose own video appears in the stored related list of a row in `rows_mask`
    (one link, forward only: a video listing yours is not thereby related to you)."""
    out = np.zeros(index.n, dtype=bool)
    if not len(index.r) or not rows_mask.any():
        return out
    owners = index.self_owner[index.c[rows_mask[index.r]]]
    out[owners[owners >= 0]] = True
    return out


def scope_mask(index: CorpusIndex, signals: "UserSignals", sub_mask: Any, seed_mask: Any) -> Any:
    """Rows `signals` may be shown, or None when scoping does not apply (catalog_scope.py):
    the claimed range, the scoped ids from SQL (provenance, followed feeds, discovered
    related), subscribed channels, the person's own seeds, and one link along the related
    lists stored on their own videos."""
    if not signals.scoped:
        return None
    claimed = index.ids <= int(signals.scope_through or 0)
    own = claimed | sub_mask | seed_mask
    own[index.rows_for_ids(signals.own_ids)] = True
    mask = own | related_rows(index, own)
    mask[index.rows_for_ids(signals.scope_ids)] = True
    return mask


def scoped_percentile(index: CorpusIndex, mask: Any) -> Any:
    """The velocity prior's percentile among `mask` rows only (others keep the library's)."""
    pct = index.prior_pct.copy() if index.prior_pct is not None else np.zeros(index.n)
    rows = np.flatnonzero(mask)
    if len(rows):
        order = np.argsort(index.prior[rows], kind="stable")
        pct[rows[order]] = (np.arange(len(rows)) + 0.5) / len(rows)
    return pct


def graph_scores(index: CorpusIndex, s: Any, graph: ScopedGraph | None = None) -> Any:
    """RP3-style walk from the normalised seed vector `s`.

    A seed also sits in the context of its own id, so a video whose related list contains
    a seed is one context away. Sparse products are np.bincount over the edge list:
    A.T @ x is bincount(c, x[r]) and A @ y is bincount(r, y[c]). With `graph` the walk runs
    over that person's scoped graph only.
    """
    n, n_ctx = index.n, index.n_ctx
    seeds = np.flatnonzero(s)
    sc = index.self_ctx[seeds]
    missing = seeds[~index.has_self[seeds]]
    base_r, base_c = (graph.r, graph.c) if graph is not None else (index.r, index.c)
    rr = np.concatenate([base_r.astype(np.int64), missing])
    cc = np.concatenate([base_c.astype(np.int64), index.self_ctx[missing]])
    deg = (graph.deg if graph is not None else index.deg) + np.bincount(seeds, minlength=n)
    k = (graph.ctx_n if graph is not None else index.ctx_n) + np.bincount(sc, minlength=n_ctx)
    if HUB == "prob":
        w = 1.0 / np.maximum(k, 1)
    else:  # idf hub, kept for parity with ref_A's switch
        w = np.log(n / np.maximum(k, 1)).clip(0)
    inv_deg = 1.0 / np.maximum(deg, 1)

    def step(x: Any) -> Any:
        mass = np.bincount(cc, weights=(x * inv_deg)[rr], minlength=n_ctx) * w
        return np.bincount(rr, weights=mass[cc], minlength=n)

    y = step(s)
    acc = y / max(y.sum(), 1e-12)
    if ROUNDS == 2:
        y2 = step(acc)
        acc = acc + SECOND_HOP_WEIGHT * y2 / max(y2.sum(), 1e-12)
    acc[seeds] = 0.0
    return acc / np.maximum(deg, 1) ** BETA


def personal_share(n_events: int) -> float:
    """Share of seed mass given to the user's own graded events: n / (n + K).

    Track B (alg/exp/B/REPORT.md, H4): shrinking personal evidence toward the prior by
    n/(n+K) beat a constant weight; K=20 was kept for OpenFeed because the real-user check
    on T1 moved -30% (ns) at K=5 and -2.8% at K=20. Without it, one watched video carried
    as much mass as a whole subscribed channel and took over the top of the feed.
    """
    return n_events / (n_events + PERSONAL_K) if n_events > 0 else 0.0


def seed_vector(index: CorpusIndex, subscribed_keys: Iterable[str], positives: dict[int, float]) -> Any:
    """Subscriptions and graded events, mixed by personal_share().

    Subscribed channels get equal mass, spread over each channel's videos. Events get mass in
    proportion to their grade. The two parts are normalised separately and mixed as
    (1 - w) * subscriptions + w * events with w = n/(n+K); a user with only one kind of
    evidence gets all mass from it.
    """
    subs = np.zeros(index.n)
    for key in subscribed_keys:
        rows = index.channel_rows(str(key).strip().lower())
        if len(rows):
            subs[rows] += 1.0 / len(rows)
    events = np.zeros(index.n)
    for video_id, weight in positives.items():
        rows = index.rows_for_ids([video_id])
        if len(rows):
            events[rows[0]] += EVENT_SEED_WEIGHT * float(weight)
    n_events = int(np.count_nonzero(events))
    st, et = subs.sum(), events.sum()
    if st <= 0 and et <= 0:
        return subs
    if st <= 0:
        return events / et
    if et <= 0:
        return subs / st
    w = personal_share(n_events)
    return (1.0 - w) * subs / st + w * events / et


def minmax(x: Any, ok: Any) -> Any:
    if not ok.any():
        return np.zeros_like(x)
    lo, hi = x[ok].min(), x[ok].max()
    return (x - lo) / max(hi - lo, 1e-12)


def feel_multiplier(index: CorpusIndex, now_ts: float | None = None, feel: dict[str, float] | None = None,
                    prior_pct: Any = None) -> Any:
    """F^beta * Q^gamma per row (feel-preregistration.md). 1.0 everywhere when feel is off.

    F = max(floor, 2^(-age/H)) makes a video need 1/F^beta times the relevance of a fresh one
    to outrank it; undated videos get the floor. Q = 0.5 + 0.5 * velocity percentile.
    """
    if feel is None:
        feel = FEEL
    if not feel or index.pub_ts is None:
        return np.ones(index.n)
    now_ts = time.time() if now_ts is None else now_ts
    age = (now_ts - index.pub_ts) / 86400.0
    fresh = np.where(np.isnan(age), feel["floor"], np.maximum(feel["floor"], np.exp2(-np.maximum(age, 0.0) / feel["half_life_days"])))
    quality = 0.5 + 0.5 * (index.prior_pct if prior_pct is None else prior_pct)
    return fresh ** feel["beta"] * quality ** feel["gamma"]


def mode_scores(mode: str, graph: Any, prior: Any, ok: Any, feel_mult: Any = None) -> Any:
    """ref_A.UserScorer.scores without the masking: graph (fuse=False) or blend (fuse=True).

    `feel_mult` (graph mode only) scales the walk score of reached videos, so freshness and
    quality reorder videos of similar relevance; reached videos still rank above unreached.
    """
    if mode == "blend":
        return W_GRAPH * minmax(graph, ok) + W_PRIOR * minmax(prior, ok)
    pr = minmax(prior, ok)
    g = graph if feel_mult is None else graph * feel_mult
    return np.where(graph > 0, 1.0 + g / max(g.max(), 1e-12), 0.0) + 1e-3 * pr


# ---- user evidence ------------------------------------------------------------------------
def build_events(
    progress: Iterable[tuple[Any, Any, Any, Any, Any]],
    feedback: Iterable[tuple[Any, Any, Any]],
    events: Iterable[tuple[Any, Any]],
) -> tuple[dict[int, float], set[int]]:
    """Port of ref_B.build_events. Returns ({video_id: weight}, negative video ids).

    progress: (video_id, total_watch_seconds, percent_completed, completed, duration_seconds).
      Watch ratio r = percent_completed if present, else seconds / duration. r < 0.5 and not
      a real completion: dropped. 0.5 <= r < 0.9: mid 0.5. r >= 0.9 or completed with
      seconds > 0: high 1.0. Completed with 0 seconds (mark watched): mid 0.5.
    feedback: (video_id, reaction, not_interested); like = 1.0, dislike / not interested = negative.
    events: (video_id, event_type); more_like_this = 1.0, hide = negative. watch_page_open is
      ignored: an open is not a positive.
    """
    w: dict[int, float] = {}
    neg: set[int] = set()
    for vid, secs, pct, completed, dur in progress:
        r = pct if pct is not None else (secs / dur if dur else 0.0)
        if (r or 0) < WEAK_RATIO and not (completed and secs and secs > 0):
            continue
        wt = W_HIGH if (completed and (secs or 0) > 0) or r >= HIGH_RATIO else W_MID
        if completed and not secs:
            wt = W_MID
        w[vid] = max(w.get(vid, 0.0), wt)
    for vid, reaction, not_interested in feedback:
        if not_interested or reaction == "dislike":
            neg.add(vid)
        elif reaction == "like":
            w[vid] = max(w.get(vid, 0.0), W_EXPLICIT)
    for vid, et in events:
        if et == "hide":
            neg.add(vid)
        elif et == "more_like_this":
            w[vid] = max(w.get(vid, 0.0), W_EXPLICIT)
    for vid in neg:
        w.pop(vid, None)
    return {int(v): x for v, x in w.items() if x > 0}, {int(v) for v in neg}


@dataclass
class UserSignals:
    """Everything about one user that ranking reads, gathered in one place."""

    subscribed_keys: set[str] = field(default_factory=set)
    positives: dict[int, float] = field(default_factory=dict)
    excluded: set[int] = field(default_factory=set)       # watched, opened, hidden, not interested, disliked
    negatives: set[int] = field(default_factory=set)      # hidden, not interested, disliked (current state)
    muted_channels: set[str] = field(default_factory=set)
    mode: str = DEFAULT_MODE
    impression_days: dict[int, int] = field(default_factory=dict)   # video id -> distinct earlier days shown
    positive_decay: dict[int, float] = field(default_factory=dict)  # video id -> recency factor (1.0 = now)
    # Feed controls (graph mode). Defaults reproduce the measured mix.
    discovery_level: float = DISCOVERY_DEFAULT
    prioritize_subscriptions: bool = True
    preferred_topic_ids: set[int] = field(default_factory=set)   # videos whose topic terms match a followed topic
    muted_topic_ids: set[int] = field(default_factory=set)       # ... a muted topic: never a pick
    takeout: dict[int, float] = field(default_factory=dict)      # imported watch -> weak seed weight (decay applied)
    explore: bool = False                                        # "Help tune recommendations": interleave graph and blend
    # Catalog scope (catalog_scope.py). Unscoped (False) only for a single-person install.
    scoped: bool = False
    scope_through: int = 0                                       # claimed range: every id up to this
    scope_ids: set[int] = field(default_factory=set)             # openfeed_video_scope above the claim
    own_ids: set[int] = field(default_factory=set)               # provenance + followed feeds above the claim


@dataclass(frozen=True)
class FeedControls:
    """Graph-mode slot shares derived from the feed controls."""

    fresh_share: float = FRESH_SLOT_SHARE    # of the non-watched slots
    channel_factor: float = 1.0              # multiplies the channels share
    wander_share: float = 0.0                # unreached "popular and recent" slots, of the non-watched slots


DEFAULT_CONTROLS = FeedControls()


def feed_controls(discovery_level: Any = DISCOVERY_DEFAULT, prioritize_subscriptions: bool = True) -> FeedControls:
    """Map "How far to wander" and "Prioritize subscriptions" onto the slot mix, within bounds.

    Discovery moves the "new from related creators" share linearly from FRESH_SHARE_MIN (0) through
    the measured FRESH_SLOT_SHARE (the default, 0.35) to FRESH_SHARE_MAX (1), and above the default
    opens up to WANDER_SHARE_MAX of slots to videos the walks never reach. Not prioritising
    subscriptions scales the channels source by NO_PRIORITY_CHANNEL_FACTOR.
    """
    try:
        d = min(1.0, max(0.0, float(discovery_level)))
    except (TypeError, ValueError):
        d = DISCOVERY_DEFAULT
    if abs(d - DISCOVERY_DEFAULT) < 1e-9:
        fresh, wander = FRESH_SLOT_SHARE, 0.0
    elif d < DISCOVERY_DEFAULT:
        fresh, wander = FRESH_SHARE_MIN + (FRESH_SLOT_SHARE - FRESH_SHARE_MIN) * d / DISCOVERY_DEFAULT, 0.0
    else:
        above = (d - DISCOVERY_DEFAULT) / (1.0 - DISCOVERY_DEFAULT)
        fresh, wander = FRESH_SLOT_SHARE + (FRESH_SHARE_MAX - FRESH_SLOT_SHARE) * above, WANDER_SHARE_MAX * above
    return FeedControls(fresh, 1.0 if prioritize_subscriptions else NO_PRIORITY_CHANNEL_FACTOR, wander)


def takeout_decay(age_seconds: float) -> float:
    """Imported watches decay slower than in-app ones (they are mostly old) but weigh less."""
    return max(TAKEOUT_DECAY_FLOOR, 2.0 ** (-max(age_seconds, 0.0) / 86400.0 / TAKEOUT_HALF_LIFE_DAYS))


def takeout_weights(rows: Iterable[dict[str, Any]], now_ts: float | None = None) -> tuple[dict[int, float], set[int]]:
    """({video id: seed weight} for the TAKEOUT_MAX_SEEDS most recent, every imported video id).

    `rows` are (id, watched_at) for imported watches found in the library. An undated watch
    gets the floor.
    """
    now_ts = time.time() if now_ts is None else now_ts
    dated: list[tuple[float, int]] = []
    every: set[int] = set()
    for row in rows:
        if row.get("id") is None:
            continue
        vid = int(row["id"])
        every.add(vid)
        dated.append((_epoch(row.get("watched_at")), vid))
    dated.sort(key=lambda item: (-item[0], item[1]))
    weights: dict[int, float] = {}
    for ts, vid in dated:
        if vid in weights:
            continue
        if len(weights) >= TAKEOUT_MAX_SEEDS:
            break
        weights[vid] = TAKEOUT_WEIGHT * (takeout_decay(now_ts - ts) if ts else TAKEOUT_DECAY_FLOOR)
    return weights, every


def event_seed_weights(signals: UserSignals) -> tuple[dict[int, float], dict[int, float]]:
    """(in-app seed weights, imported seed weights) for the watched walk.

    In-app positives keep their grade x recency decay. Imported watches the user has not also
    rated or watched here are added as weak positives, their total mass capped at
    TAKEOUT_MAX_MASS; they count toward the watched share by that mass.
    """
    decayed = {vid: w * signals.positive_decay.get(vid, 1.0) for vid, w in signals.positives.items()}
    imported = {vid: w for vid, w in signals.takeout.items()
                if vid not in decayed and vid not in signals.negatives and w > 0}
    mass = float(sum(imported.values()))
    if mass > TAKEOUT_MAX_MASS:
        scale = TAKEOUT_MAX_MASS / mass
        imported = {vid: w * scale for vid, w in imported.items()}
    return decayed, imported


def signals_from_rows(
    subscriptions: Iterable[Any],
    progress_rows: Iterable[dict[str, Any]],
    feedback_rows: Iterable[dict[str, Any]],
    event_rows: Iterable[dict[str, Any]],
    profile: dict[str, Any] | None,
    impression_rows: Iterable[dict[str, Any]] = (),
    topic_rows: Iterable[dict[str, Any]] = (),
    takeout_rows: Iterable[dict[str, Any]] = (),
) -> UserSignals:
    """Map database rows to UserSignals with the legacy ranker's exclusion semantics.

    Watched/opened (db.list_recommended_videos' watched_predicate): any mark_watched or
    watch_page_open event, or a playback row with completed, manually marked, any percent
    or any seconds. Hidden: when a feedback row exists its not_interested flag decides
    (so Undo works); otherwise any hide event. Disliked: feedback reaction 'dislike'.
    """
    profile = profile if isinstance(profile, dict) else {}
    events: dict[int, dict[str, int]] = {}
    for row in event_rows:
        vid = row.get("video_id")
        if vid is None:
            continue
        events.setdefault(int(vid), {})[str(row.get("event_type") or "")] = int(row.get("n") or 1)
    feedback = {int(row["video_id"]): row for row in feedback_rows if row.get("video_id") is not None}
    progress = [row for row in progress_rows if row.get("video_id") is not None]

    excluded: set[int] = set()
    negatives: set[int] = set()
    for vid, counts in events.items():
        if counts.get("mark_watched") or counts.get("watch_page_open"):
            excluded.add(vid)
        if vid not in feedback and (counts.get("hide") or counts.get("less_like_this")):
            negatives.add(vid)
    for row in progress:
        if (row.get("completed") or row.get("manually_marked_watched")
                or float(row.get("percent_completed") or 0) > 0 or float(row.get("total_watch_seconds") or 0) > 0):
            excluded.add(int(row["video_id"]))
    for vid, row in feedback.items():
        if row.get("not_interested") or row.get("reaction") == "dislike":
            negatives.add(vid)
    excluded |= negatives

    def pct(row: dict[str, Any]) -> Any:
        value = row.get("percent_completed")
        # The column defaults to 0 when a duration was never known: that is "no ratio",
        # not "watched none of it", so ref_B's seconds/duration fallback applies. Some rows
        # (imports, older writers) hold 0-100 instead of 0-1; anything above 1 is a percent.
        if value is None or float(value) == 0:
            return None
        value = float(value)
        return min(value / 100.0, 1.0) if value > 1.0 else value

    positives, _ = build_events(
        ((int(row["video_id"]), float(row.get("total_watch_seconds") or 0), pct(row),
          bool(row.get("completed") or row.get("manually_marked_watched")),
          float(row["duration"]) if row.get("duration") else None) for row in progress),
        ((vid, row.get("reaction"), bool(row.get("not_interested"))) for vid, row in feedback.items()),
        # A feedback row is the current state (Undo clears it), so a video that has one is not
        # also judged by its old hide events.
        ((vid, et) for vid, counts in events.items() for et in counts if not (vid in feedback and et in ("hide", "less_like_this"))),
    )
    # Recency of each positive, for decay: the latest of its progress, feedback and events.
    seen_at: dict[int, float] = {}
    for row in progress:
        ts = _epoch(row.get("last_watched_at"))
        if ts:
            seen_at[int(row["video_id"])] = max(seen_at.get(int(row["video_id"]), 0.0), ts)
    for vid, row in feedback.items():
        ts = _epoch(row.get("updated_at"))
        if ts and vid in positives:
            seen_at[vid] = max(seen_at.get(vid, 0.0), ts)
    for row in event_rows:
        vid, ts = row.get("video_id"), _epoch(row.get("last_at"))
        if vid is not None and ts and int(vid) in positives:
            seen_at[int(vid)] = max(seen_at.get(int(vid), 0.0), ts)
    now_ts = time.time()
    decay = {vid: event_decay(now_ts - seen_at[vid]) if vid in seen_at else 1.0 for vid in positives}
    muted = {str(c).strip().lower() for c in (profile.get("muted_channels") or []) if str(c).strip()}
    preferred_topic_ids: set[int] = set()
    muted_topic_ids: set[int] = set()
    for row in topic_rows:
        if row.get("id") is None:
            continue
        if row.get("muted"):
            muted_topic_ids.add(int(row["id"]))
        elif row.get("preferred"):
            preferred_topic_ids.add(int(row["id"]))
    takeout, imported_ids = takeout_weights(takeout_rows, now_ts)
    # An imported watch is a watch: it is not recommended again, whether or not it seeds.
    excluded |= imported_ids
    try:
        discovery = float(profile.get("discovery_level", DISCOVERY_DEFAULT))
    except (TypeError, ValueError):
        discovery = DISCOVERY_DEFAULT
    return UserSignals(
        subscribed_keys={str(k).strip().lower() for k in subscriptions if str(k or "").strip()},
        positives=positives,
        excluded=excluded,
        negatives=negatives,
        muted_channels=muted,
        mode=resolve_mode(profile.get("ranker_mode")),
        impression_days={int(r["video_id"]): int(r.get("days") or 0) for r in impression_rows
                         if r.get("video_id") is not None and int(r.get("days") or 0) > 0},
        positive_decay=decay,
        discovery_level=min(1.0, max(0.0, discovery)),
        prioritize_subscriptions=bool(profile.get("prioritize_subscriptions", True)),
        preferred_topic_ids=preferred_topic_ids,
        muted_topic_ids=muted_topic_ids,
        takeout=takeout,
        explore=bool(profile.get("explore_enabled", False)),
    )


def _epoch(value: Any) -> float:
    if value is None:
        return 0.0
    if hasattr(value, "timestamp"):
        try:
            return float(value.timestamp())
        except (OverflowError, OSError, ValueError):
            return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def event_decay(age_seconds: float) -> float:
    """2^(-age/EVENT_HALF_LIFE_DAYS), floored: a September watch still counts, just less."""
    return max(EVENT_DECAY_FLOOR, 2.0 ** (-max(age_seconds, 0.0) / 86400.0 / EVENT_HALF_LIFE_DAYS))


# ---- page step ----------------------------------------------------------------------------
def _topic_matrix(vectors: list[Any]) -> Any:
    vocab: dict[str, int] = {}
    entries: list[tuple[int, int, float]] = []
    for i, vector in enumerate(vectors):
        if isinstance(vector, dict):
            for term, weight in vector.items():
                try:
                    value = float(weight)
                except (TypeError, ValueError):
                    continue
                if value:
                    entries.append((i, vocab.setdefault(str(term), len(vocab)), value))
    X = np.zeros((len(vectors), max(len(vocab), 1)))
    for i, j, value in entries:
        X[i, j] = value
    norms = np.sqrt((X * X).sum(axis=1))
    return X / np.maximum(norms, 1e-12)[:, None]


def mmr_page(scores: Any, channels: Any, vectors: list[Any], slots: int = MMR_SLOTS) -> list[int]:
    """Greedy page over a score-sorted window (best first). Returns positions into it.

    Each slot takes argmax of MMR_LAMBDA * rel * 0.8^k - (1 - MMR_LAMBDA) * maxsim, where rel
    is min-max within the window, k is how many picks that channel already has, and maxsim
    is the topic cosine to the picks so far. After `slots` picks the rest keep score order.
    Ties resolve to the earlier position, so the page is deterministic.
    """
    m = len(scores)
    if not m:
        return []
    scores = np.asarray(scores, dtype=np.float64)
    rel = (scores - scores.min()) / max(scores.max() - scores.min(), 1e-12)
    X = _topic_matrix(vectors)
    S = X @ X.T
    channels = list(channels)
    taken_per_channel: dict[Any, int] = {}
    avail = np.ones(m, dtype=bool)
    maxsim = np.zeros(m)
    chosen: list[int] = []
    for _ in range(min(slots, m)):
        k = np.array([taken_per_channel.get(ch, 0) for ch in channels], dtype=np.float64)
        value = MMR_LAMBDA * rel * MMR_CHANNEL_DECAY ** k - (1 - MMR_LAMBDA) * maxsim
        value[~avail] = -np.inf
        j = int(np.argmax(value))
        chosen.append(j)
        avail[j] = False
        taken_per_channel[channels[j]] = taken_per_channel.get(channels[j], 0) + 1
        maxsim = np.maximum(maxsim, S[j])
    return chosen + [i for i in range(m) if avail[i]]


# ---- per-user ranking -----------------------------------------------------------------------
@dataclass
class LaneRanking:
    rows: Any            # index rows, final order
    scores: Any          # score per row in `rows`
    source: Any = None   # graph mode: index into SOURCES per row in `rows`, -1 = unreached remainder
    chan_best: Any = None  # graph mode: per channel code, its best-linked row (for fresh reasons), -1 none
    teams: Any = None      # interleaved lanes: team per row (index into INTERLEAVE_TEAMS)


@dataclass
class UserRanking:
    key: tuple[Any, ...]
    mode: str
    signals: UserSignals
    s: Any = None
    graph: Any = None
    sub_mask: Any = None
    event_seed_mask: Any = None
    base_ok: Any = None
    base_ok_shorts: Any = None    # the same exclusions, Shorts instead of long videos
    demoted: Any = None
    feel_mult: Any = None
    g_sub: Any = None             # walk from subscriptions only
    g_evt: Any = None             # walk from graded events only
    s_sub: Any = None
    s_evt: Any = None
    watched_share: float = 0.0
    seed_of: Any = None           # int64[n] event seed row contributing most on the first hop, -1 none
    rotation: Any = None          # float64[n] ROTATION_DECAY^k, 1.0 for unseen
    topic_mult: Any = None        # float64[n] PREFERRED_TOPIC_BOOST on followed-topic videos, else 1.0
    controls: FeedControls = DEFAULT_CONTROLS
    scope: Any = None             # bool[n] rows this person may be shown, None = unscoped
    walk_graph: Any = None        # ScopedGraph over `scope`, None = the whole library
    prior_pct: Any = None         # velocity percentile within `scope`, None = the library's
    unscoped_ok: Any = None       # base_ok before the scope, for Up next's own related list
    lanes: dict[str, LaneRanking] = field(default_factory=dict)
    views: dict[str, "UserRanking"] = field(default_factory=dict)   # the same user ranked in another mode
    computed_seconds: float = 0.0


def prepare_user(index: CorpusIndex, signals: UserSignals, key: tuple[Any, ...] = ()) -> UserRanking:
    started = time.perf_counter()
    entry = UserRanking(key=key, mode=signals.mode, signals=signals)
    if signals.mode == "classic":
        return entry
    n = index.n
    sub_mask = np.zeros(n, dtype=bool)
    for key_ in signals.subscribed_keys:
        sub_mask[index.channel_rows(key_)] = True
    decayed, imported = event_seed_weights(signals)
    event_seed_mask = np.zeros(n, dtype=bool)
    event_seed_mask[index.rows_for_ids(signals.positives)] = True
    event_seed_mask[index.rows_for_ids(imported)] = True
    # Everything below reads only the rows this person may be shown (catalog_scope.py):
    # the walk runs over their part of the graph and the candidates are cut to it.
    scope = scope_mask(index, signals, sub_mask, event_seed_mask)
    walk_graph = scoped_graph(index, scope) if scope is not None else None
    entry.scope, entry.walk_graph = scope, walk_graph
    entry.prior_pct = scoped_percentile(index, scope) if scope is not None else None
    s = seed_vector(index, signals.subscribed_keys, signals.positives)
    s_sub = seed_vector(index, signals.subscribed_keys, {})
    s_evt = seed_vector(index, set(), {**decayed, **imported})
    g_sub = graph_scores(index, s_sub, walk_graph) if s_sub.any() else np.zeros(n)
    g_evt = graph_scores(index, s_evt, walk_graph) if s_evt.any() else np.zeros(n)
    # Blend mode is the sealed RC2 and keeps the mass-mixed walk it was tested with.
    graph = graph_scores(index, s, walk_graph) if signals.mode == "blend" and s.any() else g_sub + g_evt
    real_rows = index.rows_for_ids(signals.positives)
    n_eff = float(sum(signals.positive_decay.get(int(v), 1.0) for v in index.ids[real_rows]))
    # Imported watches count toward the watched share by their (capped) mass, not their number.
    n_eff += float(sum(imported[int(v)] for v in index.ids[index.rows_for_ids(imported)]))
    entry.g_sub, entry.g_evt, entry.s_sub, entry.s_evt = g_sub, g_evt, s_sub, s_evt
    entry.watched_share = min(WATCHED_SHARE_MAX, n_eff / (n_eff + PERSONAL_SLOT_K)) if n_eff and g_evt.any() else 0.0
    entry.seed_of = _seed_attribution(index, s_evt, g_evt) if g_evt.any() else None
    allowed = ~event_seed_mask
    if index.dead is not None:
        allowed &= ~index.dead
    allowed[index.rows_for_ids(signals.excluded)] = False
    allowed[index.rows_for_ids(signals.muted_topic_ids)] = False
    if signals.muted_channels:
        muted_codes = [index.chan_lookup[k] for k in signals.muted_channels if k in index.chan_lookup]
        if muted_codes:
            allowed &= ~np.isin(index.chan, muted_codes)
    entry.unscoped_ok = allowed & ~index.blocked
    if scope is not None:
        allowed &= scope
    base_ok = allowed & ~index.blocked
    # The Shorts lane is the one place Shorts are candidates; live streams never are.
    entry.base_ok_shorts = allowed & index.short & ~index.live
    demoted = np.zeros(n, dtype=bool)
    neg_rows = index.rows_for_ids(signals.negatives)
    if len(neg_rows):
        codes, counts = np.unique(index.chan[neg_rows], return_counts=True)
        bad = codes[counts >= CHANNEL_MIN_NEGATIVES]
        if len(bad):
            demoted = np.isin(index.chan, bad)
    entry.s, entry.graph, entry.sub_mask, entry.event_seed_mask = s, graph, sub_mask, event_seed_mask
    entry.base_ok, entry.demoted = base_ok, demoted
    entry.feel_mult = feel_multiplier(index, prior_pct=entry.prior_pct)
    topic_mult = np.ones(n)
    topic_mult[index.rows_for_ids(signals.preferred_topic_ids)] = PREFERRED_TOPIC_BOOST
    entry.topic_mult = topic_mult
    entry.controls = feed_controls(signals.discovery_level, signals.prioritize_subscriptions)
    rotation = np.ones(n)
    shown = signals.impression_days
    if shown:
        rows = index.rows_for_ids(list(shown))
        days = np.asarray([min(int(shown[int(i)]), ROTATION_MAX_DAYS) for i in index.ids[rows]], dtype=np.float64)
        rotation[rows] = ROTATION_DECAY ** days
    entry.rotation = rotation
    entry.computed_seconds = time.perf_counter() - started
    return entry


def lane_mask(index: CorpusIndex, entry: UserRanking, lane: str, now_ts: float | None = None) -> Any:
    """Eligible rows of a lane: the shared exclusions, then the lane's own rule.

    recommended          not from a subscribed channel, not a seed
    recommended-recent   the same, published within RECENT_MAX_DAYS
    recommended-long     the same, at least LONG_FORM_MIN_SECONDS long
    shorts               Shorts only (live never), any channel
    all                  everything eligible
    """
    if lane == "shorts":
        ok = entry.base_ok_shorts.copy()
    else:
        ok = entry.base_ok.copy()
    if lane in ("recommended", *FILTERED_LANES):
        # Subscribed channels have their own shelves; seeds are never candidates (ref_A).
        ok &= ~entry.sub_mask & (entry.s == 0)
    if lane == "recommended-recent":
        now_ts = time.time() if now_ts is None else now_ts
        age = (now_ts - index.pub_ts) / 86400.0 if index.pub_ts is not None else np.full(index.n, np.nan)
        ok &= np.nan_to_num(age, nan=np.inf) <= RECENT_MAX_DAYS
    elif lane == "recommended-long":
        dur = index.dur if index.dur is not None else np.full(index.n, np.nan)
        ok &= np.nan_to_num(dur, nan=0.0) >= LONG_FORM_MIN_SECONDS
    return ok


def lane_scores(index: CorpusIndex, entry: UserRanking, lane: str) -> tuple[Any, Any]:
    """(score per row with -inf for ineligible, eligibility mask) for one lane."""
    ok = lane_mask(index, entry, lane)
    f = mode_scores(entry.mode, entry.graph, index.prior, ok, entry.feel_mult if entry.mode == "graph" else None)
    f = np.where(entry.demoted, f * CHANNEL_DEMOTION, f)
    if entry.rotation is not None:
        # Scaling the whole score keeps reached-above-unreached in graph mode only roughly;
        # a 3-day repeat (0.42) can drop below a fresh unreached video, which is the intent.
        f = f * entry.rotation
    return np.where(ok, f, -np.inf), ok


def _ordered(score: Any, mask: Any, depth: int) -> list[int]:
    rows = np.flatnonzero(mask)
    if len(rows) > depth:
        rows = rows[np.argpartition(-score[rows], depth - 1)[:depth]]
    return rows[np.lexsort((rows, -score[rows]))].tolist()


def slot_mix(lists: list[list[int]], shares: list[float], depth: int) -> tuple[list[int], list[int]]:
    """Smooth weighted round-robin: each slot goes to the source furthest below its share.
    A row already taken by another source is skipped; a source that runs dry drops out."""
    shares = [max(0.0, float(x)) for x in shares]
    out: list[int] = []
    src: list[int] = []
    seen: set[int] = set()
    ptr = [0] * len(lists)
    taken = [0] * len(lists)
    while len(out) < depth:
        best, pick = None, -1
        for i, lst in enumerate(lists):
            while ptr[i] < len(lst) and lst[ptr[i]] in seen:
                ptr[i] += 1
            if shares[i] <= 0 or ptr[i] >= len(lst):
                continue
            need = shares[i] * (len(out) + 1) - taken[i]
            if best is None or need > best:
                best, pick = need, i
        if pick < 0:
            break
        row = lists[pick][ptr[pick]]
        ptr[pick] += 1
        seen.add(row)
        out.append(row)
        src.append(pick)
        taken[pick] += 1
    return out, src


def _seed_attribution(index: CorpusIndex, s_evt: Any, g_evt: Any) -> Any:
    """For every row, the event seed giving it the most first-hop mass (seed mass / context
    size), or -1. Vectorised per seed: a seed touches only its own contexts' members."""
    best = np.full(index.n, -1, dtype=np.int64)
    best_mass = np.zeros(index.n)
    for seed in np.flatnonzero(s_evt):
        ctxs = list(index.c[index.row_ptr[seed]:index.row_ptr[seed + 1]]) + [int(index.self_ctx[seed])]
        for ctx in set(int(c) for c in ctxs):
            members = index.ctx_rows[index.ctx_ptr[ctx]:index.ctx_ptr[ctx + 1]]
            owner = int(index.self_owner[ctx])
            if owner >= 0:
                members = np.append(members, owner)
            mass = float(s_evt[seed]) / max(float(index.ctx_n[ctx]), 1.0)
            better = members[best_mass[members] < mass]
            best_mass[better] = mass
            best[better] = seed
    best[g_evt <= 0] = -1
    return best


def _round_robin_by_seed(order: list[int], seed_of: Any) -> list[int]:
    """Interleave the watched list across the seeds it came from, best first within each, so
    one watched video cannot fill the shelf (audit: 7 of 13 watched slots from one seed)."""
    queues: dict[int, list[int]] = {}
    firsts: list[int] = []
    for row in order:
        seed = int(seed_of[row]) if seed_of is not None else -1
        if seed not in queues:
            queues[seed] = []
            firsts.append(seed)
        queues[seed].append(row)
    out: list[int] = []
    level = 0
    while len(out) < len(order):
        for seed in firsts:
            if level < len(queues[seed]):
                out.append(queues[seed][level])
        level += 1
    return out


def creator_affinity(index: CorpusIndex, g: Any, ok: Any) -> tuple[Any, Any]:
    """Per channel code: sum of its CREATOR_TOP_VIDEOS best walk scores among `ok` rows, and the
    row of its best one (-1 when the channel is not reached)."""
    n_ch = len(index.chan_keys)
    rows = np.flatnonzero(ok & (g > 0))
    if not len(rows):
        return np.zeros(n_ch), np.full(n_ch, -1, dtype=np.int64)
    order = rows[np.lexsort((-g[rows], index.chan[rows]))]
    ch = index.chan[order]
    first = np.r_[0, np.flatnonzero(np.diff(ch)) + 1]
    rank_in = np.arange(len(order)) - np.repeat(first, np.diff(np.r_[first, len(order)]))
    top = order[rank_in < CREATOR_TOP_VIDEOS]
    affinity = np.bincount(index.chan[top], weights=g[top], minlength=n_ch)
    best = np.full(n_ch, -1, dtype=np.int64)
    best[ch[first]] = order[first]
    return affinity, best


def graph_mix(index: CorpusIndex, g_sub: Any, g_evt: Any, ok: Any, mult: Any, watched_share: float,
              now_ts: float | None = None, depth: int = RANK_DEPTH, seed_of: Any = None,
              with_creators: bool = False, controls: FeedControls = DEFAULT_CONTROLS, prior_pct: Any = None) -> Any:
    """Graph-mode order: the three-source slot mix. Returns (rows, source index per row).

    `mult` is the per-row factor applied inside every source (quality, rotation, demotion).
    The channels list ends with the unreached remainder by the prior, so the lane never runs
    dry; those rows get source -1. `controls` sets the shares (feed_controls); the default is
    the measured mix, and a wander share above 0 gives the unreached remainder its own slots.
    """
    now_ts = time.time() if now_ts is None else now_ts
    if index.n == 0:
        empty = np.zeros(0, dtype=np.int64)
        return (empty, empty, np.zeros(0, dtype=np.int64)) if with_creators else (empty, empty)
    reached_sub = ok & (g_sub > 0)
    reached_evt = ok & (g_evt > 0)
    age = (now_ts - index.pub_ts) / 86400.0 if index.pub_ts is not None else np.full(index.n, np.inf)
    norm = g_sub / max(float(g_sub.max()), 1e-300) + g_evt / max(float(g_evt.max()), 1e-300)
    watched = _ordered(g_evt * mult, reached_evt, depth) if watched_share > 0 else []
    if watched and seed_of is not None:
        watched = _round_robin_by_seed(watched, seed_of)
    # New from related creators: a creator the walks link strongly has its recent uploads
    # shown even when those uploads are too new to be linked themselves.
    affinity, chan_best = creator_affinity(index, norm, ok)
    fresh_mask = ok & (age <= FRESH_MAX_DAYS) & (affinity[index.chan] > 0)
    fresh_score = affinity[index.chan] * mult * np.exp2(-np.maximum(age, 0.0) / FRESH_MAX_DAYS)
    fresh = _round_robin_by_seed(_ordered(np.nan_to_num(fresh_score, nan=0.0), fresh_mask, depth), index.chan)
    channels = _ordered(g_sub * mult, reached_sub, depth)
    pct = index.prior_pct if prior_pct is None else prior_pct
    remainder = _ordered(pct * mult, ok & ~reached_sub & ~reached_evt, depth)
    rest = 1.0 - watched_share
    # Shares of the non-watched slots. Not prioritising subscriptions hands part of the channels
    # share to the creators source, and wandering takes its slots from the channels share, so the
    # watched share (and its cap) is never touched.
    fresh_r = controls.fresh_share + (1 - controls.fresh_share) * (1 - controls.channel_factor)
    wander_r = controls.wander_share
    channels_r = max(0.0, 1 - fresh_r - wander_r)
    shares = [watched_share, fresh_r * rest, channels_r * rest, wander_r * rest]
    lists = [watched, fresh, channels + remainder, remainder if controls.wander_share > 0 else []]
    rows, src = slot_mix(lists, shares, depth)
    unreached = set(remainder)
    src = [-1 if (s_ in (2, 3) and r in unreached) else s_ for r, s_ in zip(rows, src)]
    out = np.asarray(rows, dtype=np.int64), np.asarray(src, dtype=np.int64)
    return (*out, chan_best) if with_creators else out


def _graph_mult(index: CorpusIndex, entry: UserRanking) -> Any:
    """The per-row factor every graph-mode source applies: quality, rotation, demotion, topics."""
    mult = (entry.feel_mult if entry.feel_mult is not None else 1.0) * (entry.rotation if entry.rotation is not None else 1.0)
    if entry.topic_mult is not None:
        mult = mult * entry.topic_mult
    mult = np.where(entry.demoted, mult * CHANNEL_DEMOTION, mult) if entry.demoted is not None else mult
    return np.broadcast_to(mult, (index.n,))


def rank_shorts(index: CorpusIndex, entry: UserRanking, ok: Any, now_ts: float | None = None) -> LaneRanking:
    """The Shorts shelf: Shorts the walks reach, by (max-normalised channels walk + watched walk)
    x quality x rotation x topics; then the unreached ones newest first, which is what the shelf
    showed before. Source 2 = reached, -1 = the newest-first remainder."""
    now_ts = time.time() if now_ts is None else now_ts
    g_sub = entry.g_sub if entry.g_sub is not None else np.zeros(index.n)
    g_evt = entry.g_evt if entry.g_evt is not None else np.zeros(index.n)
    walk = g_sub / max(float(g_sub.max()), 1e-300) + g_evt / max(float(g_evt.max()), 1e-300)
    reached = ok & (walk > 0)
    reached_rows = _ordered(walk * _graph_mult(index, entry), reached, RANK_DEPTH)
    pub = np.nan_to_num(index.pub_ts, nan=-np.inf) if index.pub_ts is not None else np.zeros(index.n)
    rest_rows = _ordered(pub, ok & ~reached, RANK_DEPTH)
    rows = np.asarray(reached_rows + rest_rows, dtype=np.int64)[:RANK_DEPTH]
    scores = 1.0 - np.arange(len(rows)) / max(len(rows), 1)
    source = np.asarray([2] * len(reached_rows) + [-1] * len(rest_rows), dtype=np.int64)[:RANK_DEPTH]
    return LaneRanking(rows=rows, scores=scores, source=source)


def rank_lane(index: CorpusIndex, entry: UserRanking, lane: str, topic_vectors: Any = None) -> LaneRanking:
    """Score order, then the MMR page over the top MMR_TOP. `topic_vectors(ids) -> {id: vector}`."""
    f, ok = lane_scores(index, entry, lane)
    if lane == "shorts":
        return rank_shorts(index, entry, ok)
    if entry.mode == "graph" and entry.g_sub is not None:
        mult = _graph_mult(index, entry)
        order, source, chan_best = graph_mix(index, entry.g_sub, entry.g_evt, ok, mult,
                                             entry.watched_share, seed_of=entry.seed_of, with_creators=True,
                                             controls=entry.controls, prior_pct=entry.prior_pct)
        if not len(order):
            return LaneRanking(np.zeros(0, dtype=np.int64), np.zeros(0), np.zeros(0, dtype=np.int64))
        # The mix has no single score; rank position stands in (1.0 first), so the MMR step
        # trades position against variety exactly as it traded score.
        f = np.full(index.n, -np.inf)
        f[order] = 1.0 - np.arange(len(order)) / max(len(order), 1)
        src_of = dict(zip(order.tolist(), source.tolist()))
        head = order[:MMR_TOP]
        vectors_by_id = topic_vectors([int(i) for i in index.ids[head]]) if topic_vectors else {}
        picks = mmr_page(f[head], index.chan[head].tolist(), [vectors_by_id.get(int(i)) for i in index.ids[head]])
        rows = np.concatenate([head[picks], order[MMR_TOP:]])
        return LaneRanking(rows=rows, scores=f[rows], source=np.asarray([src_of[int(r)] for r in rows], dtype=np.int64),
                           chan_best=chan_best)
    eligible = np.flatnonzero(ok)
    depth = min(RANK_DEPTH, len(eligible))
    if depth == 0:
        return LaneRanking(np.zeros(0, dtype=np.int64), np.zeros(0))
    if depth < len(eligible):
        part = eligible[np.argpartition(-f[eligible], depth - 1)[:depth]]
    else:
        part = eligible
    order = part[np.lexsort((part, -f[part]))]       # score desc, row asc on ties
    head = order[:MMR_TOP]
    vectors_by_id = topic_vectors([int(i) for i in index.ids[head]]) if topic_vectors else {}
    picks = mmr_page(
        f[head],
        index.chan[head].tolist(),
        [vectors_by_id.get(int(i)) for i in index.ids[head]],
    )
    rows = np.concatenate([head[picks], order[MMR_TOP:]])
    return LaneRanking(rows=rows, scores=f[rows])


def top_contributors(index: CorpusIndex, entry: UserRanking, row: int) -> tuple[int, int]:
    """(event seed row, subscribed channel code) contributing most to `row` on the first hop,
    -1 when none. Contribution of a seed through a shared context = seed mass / context size."""
    best_evt, best_evt_mass = -1, 0.0
    chan_mass: dict[int, float] = {}
    s_sub = entry.s_sub if entry.s_sub is not None else entry.s
    s_evt = entry.s_evt if entry.s_evt is not None else np.zeros(index.n)
    for ctx in index.c[index.row_ptr[row]:index.row_ptr[row + 1]]:
        members = list(index.ctx_rows[index.ctx_ptr[ctx]:index.ctx_ptr[ctx + 1]])
        owner = int(index.self_owner[ctx])
        if owner >= 0:
            members.append(owner)
        size = max(float(index.ctx_n[ctx]), 1.0)
        for member in members:
            member = int(member)
            if member == row:
                continue
            if s_evt[member] > 0 and s_evt[member] / size > best_evt_mass:
                best_evt, best_evt_mass = member, float(s_evt[member] / size)
            if s_sub[member] > 0:
                code = int(index.chan[member])
                chan_mass[code] = chan_mass.get(code, 0.0) + float(s_sub[member] / size)
    best_chan = max(chan_mass, key=chan_mass.get) if chan_mass else -1
    return best_evt, best_chan


def explain_parts(index: CorpusIndex, entry: UserRanking, row: int, source: int, chan_best: Any = None) -> tuple[str, Any]:
    """(kind, reference) for a reason; references are resolved to titles by the caller.

    kinds: "watched" (seed video id), "follow" (channel key), "follow_near" (channel key, two
    hops), "subscribed", "popular". `source` is the slot-mix source (-1 for the remainder).
    """
    if entry.sub_mask is not None and entry.sub_mask[row]:
        return "subscribed", None
    if source == 1 and chan_best is not None:
        # New from a related creator: explain through that creator's best-linked video.
        anchor = int(chan_best[int(index.chan[row])])
        if anchor >= 0:
            evt, chan = top_contributors(index, entry, anchor)
            if chan >= 0:
                return "similar", index.chan_keys[chan]
            if evt >= 0:
                return "watched", int(index.ids[evt])
        return "follow_near", None
    if source == -1 or (entry.graph is not None and entry.graph[row] <= 0):
        return "popular", None
    evt, chan = top_contributors(index, entry, row)
    prefer_watched = source == 0 or (source == 1 and evt >= 0 and (entry.g_evt is not None and entry.g_sub is not None
                                     and entry.g_evt[row] / max(float(entry.g_evt.max()), 1e-300) >= entry.g_sub[row] / max(float(entry.g_sub.max()), 1e-300)))
    if prefer_watched and evt >= 0:
        return "watched", int(index.ids[evt])
    if chan >= 0:
        return "follow", index.chan_keys[chan]
    if evt >= 0:
        return "watched", int(index.ids[evt])
    return "follow_near", None


def reason_text(kind: str, label: str | None, fresh: bool = False) -> str:
    label = (label or "").strip()
    if len(label) > 60:
        label = label[:57].rstrip() + "…"
    text = {
        "subscribed": "From a channel you follow",
        "popular": "Popular and recent",
        "follow_near": "Close to channels you follow",
        "watched": f"Because you watched “{label}”" if label else "Because of a video you watched",
        "follow": f"Because you follow {label}" if label else "Linked from a channel you follow",
        "similar": f"Similar to {label}" if label else "Close to channels you follow",
    }.get(kind, "Recommended")
    return f"New · {text}" if fresh and kind not in ("subscribed",) else text


def explain(index: CorpusIndex, entry: UserRanking, row: int) -> str:
    """A one-line reason from the graph's first hop: which seed videos share a context."""
    if entry.sub_mask is not None and entry.sub_mask[row]:
        return "From a channel you follow"
    if entry.graph is None or entry.graph[row] <= 0:
        return "Popular and recent"
    sub_links: set[int] = set()
    event_links: set[int] = set()
    seeds = entry.s
    for ctx in index.c[index.row_ptr[row]:index.row_ptr[row + 1]]:
        members = list(index.ctx_rows[index.ctx_ptr[ctx]:index.ctx_ptr[ctx + 1]])
        owner = int(index.self_owner[ctx])
        if owner >= 0:
            members.append(owner)
        for member in members:
            member = int(member)
            if member == row or seeds[member] <= 0:
                continue
            if entry.event_seed_mask[member]:
                event_links.add(member)
            if entry.sub_mask[member]:
                sub_links.add(member)
    # Name whichever side carried more seed mass into this video's first hop, not events by
    # default: a video linked from one finished video and thirty subscription videos is mostly
    # there because of the subscriptions.
    event_mass = float(sum(seeds[m] for m in event_links))
    sub_mass = float(sum(seeds[m] for m in sub_links - event_links))
    if event_links and event_mass >= sub_mass:
        count = len(event_links)
        return f"Linked from {count} video{'s' if count != 1 else ''} you watched or liked"
    if sub_links:
        count = len(sub_links)
        channels = len({int(index.chan[m]) for m in sub_links})
        whose = "a channel you follow" if channels == 1 else f"{channels} channels you follow"
        return f"Linked from {count} video{'s' if count != 1 else ''} of {whose}"
    return "Two links away from channels you follow"


# ---- process-wide state: the index and its rebuilds ---------------------------------------
_LIGHT_SQL = """
SELECT v.id,
       ('x' || substr(md5(v.youtube_video_id), 1, 16))::bit(64)::bigint AS yh,
       lower(btrim(COALESCE(NULLIF(btrim(c.youtube_channel_id), ''), NULLIF(btrim(c.title), ''), v.author, ''))) AS ck,
       v.view_count,
       extract(epoch FROM v.published_at)::float8 AS pub,
       v.duration_seconds::float8 AS dur,
       strpos(COALESCE(v.url, ''), '/shorts/') > 0 AS short_url,
       extract(epoch FROM v.updated_at)::float8 AS upd
FROM videos v
LEFT JOIN channels c ON c.id = v.channel_id
ORDER BY v.id
"""

# metadata is ~17 KB of jsonb per video, so reading it is the expensive part of a build
# (~20 s for the whole library). It is read once per process, then only for rows whose
# updated_at moved.
_HEAVY_SQL = """
SELECT v.id,
       COALESCE(v.metadata->>'liveNow', '') = 'true' AS live,
       ARRAY(
         SELECT ('x' || substr(md5(e->>'videoId'), 1, 16))::bit(64)::bigint
         FROM jsonb_array_elements(
           CASE WHEN jsonb_typeof(v.metadata->'recommendedVideos') = 'array'
                THEN v.metadata->'recommendedVideos' ELSE '[]'::jsonb END) e
         WHERE e->>'videoId' IS NOT NULL
       ) AS rel,
       extract(epoch FROM v.updated_at)::float8 AS upd
FROM videos v
"""


def _release_freed_memory() -> None:
    """Hand the build's temporaries back to the OS. glibc keeps freed heap otherwise, which
    left a long-running web process ~100 MB larger after each rebuild (measured on dev)."""
    try:
        import ctypes

        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:  # noqa: BLE001 - not glibc, nothing to do
        pass


def _merge_related(ids: Any, upd: Any, keep: Any, pos: Any, old: dict[str, Any] | None,
                   fresh: dict[int, tuple[bool, bytes]]) -> dict[str, Any]:
    """New flat related arrays: rows in `keep` copy their slice from `old`, the rest come
    from `fresh` (a row missing from both, e.g. deleted mid-build, gets no links)."""
    n = len(ids)
    lengths = np.zeros(n, dtype=np.int64)
    live = np.zeros(n, dtype=bool)
    if old is not None and keep.any():
        old_len = np.diff(old["ptr"])
        lengths[keep] = old_len[pos[keep]]
        live[keep] = old["live"][pos[keep]]
    stale_rows = np.flatnonzero(~keep)
    for row in stale_rows:
        item = fresh.get(int(ids[row]))
        if item is not None:
            live[row] = item[0]
            lengths[row] = len(item[1]) // 8
    ptr = np.zeros(n + 1, dtype=np.int64)
    np.cumsum(lengths, out=ptr[1:])
    keys = np.empty(int(ptr[-1]), dtype=np.int64)
    if old is not None and keep.any():
        kept = np.flatnonzero(keep)
        lens = lengths[kept]
        src_start = old["ptr"][pos[kept]]
        dst_start = ptr[kept]
        total = int(lens.sum())
        within = np.arange(total) - np.repeat(np.cumsum(lens) - lens, lens)
        keys[np.repeat(dst_start, lens) + within] = old["keys"][np.repeat(src_start, lens) + within]
    for row in stale_rows:
        item = fresh.get(int(ids[row]))
        if item is not None and item[1]:
            keys[ptr[row]:ptr[row + 1]] = np.frombuffer(item[1], dtype=np.int64)
    return {"ids": ids, "upd": upd, "live": live, "ptr": ptr, "keys": keys}


class IndexManager:
    def __init__(self) -> None:
        self.index: CorpusIndex | None = None
        self._lock = threading.Lock()
        self._building = False
        self._last_check = 0.0
        self._version = 0
        # Related lists between builds: ids, upd, live, ptr, keys (see _merge_related). Also
        # saved to disk, so a restart re-reads only changed rows (~2 s) instead of every
        # video's metadata (~40 s, during which the classic ranker serves).
        self._related_arrays: dict[str, Any] | None = None
        self._loaded_cache = False
        self.last_error = ""
        self.state = "not built"

    def current(self) -> CorpusIndex | None:
        """The index to serve now. Never blocks; schedules a rebuild when one is due."""
        self.maybe_refresh()
        return self.index

    def maybe_refresh(self, force: bool = False) -> None:
        now = time.time()
        with self._lock:
            if self._building:
                return
            if not force and self.index is not None and now - self._last_check < INDEX_CHECK_INTERVAL_SECONDS:
                return
            self._building = True
            self._last_check = now
        threading.Thread(target=self._refresh, name="ranker-index", daemon=True).start()

    def _refresh(self) -> None:
        try:
            if self.index is None or self._stale():
                self.state = "building" if self.index is None else "rebuilding"
                self.rebuild()
            self.state = "ready" if self.index is not None else self.state
        except Exception as exc:  # noqa: BLE001 - the legacy ranker keeps serving
            self.last_error = f"{type(exc).__name__}: {exc}"
            self.state = "failed" if self.index is None else "ready (last rebuild failed)"
            logger.warning("ranker index build failed: %s", self.last_error)
        finally:
            with self._lock:
                self._building = False

    def _stale(self) -> bool:
        from . import db

        index = self.index
        if index is None:
            return True
        if time.time() - index.built_at >= INDEX_MAX_AGE_SECONDS:
            return True
        with db.connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n, COALESCE(MAX(id), 0) AS max_id FROM videos").fetchone()
        return int(row["n"]) != index.video_count or int(row["max_id"]) != index.max_id

    def rebuild(self) -> CorpusIndex:
        """Read the library and swap in a new index. The old index serves until the swap.

        Rows stream through server-side cursors into preallocated arrays, and the related
        lists are kept between builds as one flat array (CSR), so a rebuild holds a few MB
        of temporaries rather than a Python object per video.
        """
        from psycopg.rows import tuple_row

        from . import db

        started = time.perf_counter()
        with db.connect() as conn:
            n = int(conn.execute("SELECT COUNT(*) AS n FROM videos").fetchone()["n"])
            cap = n + 1024  # rows inserted between the count and the scan
            ids = np.empty(cap, dtype=np.int64)
            yh = np.empty(cap, dtype=np.int64)
            views = np.empty(cap)
            pub = np.empty(cap)
            dur = np.empty(cap)
            short_url = np.empty(cap, dtype=bool)
            upd = np.empty(cap)
            chan_codes = np.empty(cap, dtype=np.int32)
            chan_names: dict[str, int] = {}
            nan = float("nan")
            count = 0
            with conn.cursor(name="ranker_light", row_factory=tuple_row) as cur:
                cur.itersize = 5000
                cur.execute(_LIGHT_SQL)
                for row in cur:
                    if count == cap:
                        break
                    ids[count] = row[0]
                    yh[count] = row[1]
                    chan_codes[count] = chan_names.setdefault(row[2] or "", len(chan_names))
                    views[count] = nan if row[3] is None else row[3]
                    pub[count] = nan if row[4] is None else row[4]
                    dur[count] = nan if row[5] is None else row[5]
                    short_url[count] = bool(row[6])
                    upd[count] = row[7] or 0.0
                    count += 1
            ids, yh, views, pub, dur, short_url, upd, chan_codes = (
                a[:count] for a in (ids, yh, views, pub, dur, short_url, upd, chan_codes)
            )
            if self._related_arrays is None and not self._loaded_cache:
                self._loaded_cache = True
                self._related_arrays = _load_related_cache()
            old = self._related_arrays
            if old is not None and len(old["ids"]):
                pos = np.clip(np.searchsorted(old["ids"], ids), 0, len(old["ids"]) - 1)
                keep = (old["ids"][pos] == ids) & (old["upd"][pos] == upd)
            else:
                pos = np.zeros(count, dtype=np.int64)
                keep = np.zeros(count, dtype=bool)
            stale_ids = ids[~keep]
            fresh: dict[int, tuple[bool, bytes]] = {}
            if len(stale_ids):
                with conn.cursor(name="ranker_related", row_factory=tuple_row) as cur:
                    cur.itersize = 2000
                    if len(stale_ids) > count // 2:
                        cur.execute(_HEAVY_SQL)
                    else:
                        cur.execute(_HEAVY_SQL + " WHERE v.id = ANY(%s)", ([int(i) for i in stale_ids],))
                    for vid, live, rel, _upd in cur:
                        fresh[int(vid)] = (bool(live), np.asarray(rel or (), dtype=np.int64).tobytes())
        related = _merge_related(ids, upd, keep, pos, old, fresh)
        rel_len = np.diff(related["ptr"])
        names = [""] * len(chan_names)
        for name, code in chan_names.items():
            names[code] = name
        self._version += 1
        index = build_index(
            ids=ids,
            self_key=yh,
            rel_rows=np.repeat(np.arange(count, dtype=np.int64), rel_len),
            rel_keys=related["keys"],
            channel_keys=[names[code] for code in chan_codes],
            view_count=views,
            published_ts=pub,
            duration_seconds=dur,
            live_flag=related["live"],
            short_url=short_url,
            now_ts=time.time(),
            version=self._version,
        )
        index.build_seconds = time.perf_counter() - started
        self._related_arrays = related
        if len(stale_ids):
            _save_related_cache(related)
        self.index = index
        self.last_error = ""
        logger.info("ranker index v%s: %s videos, %s contexts, %s links, %.1fs (%s metadata rows read)",
                    index.version, index.n, index.n_ctx, len(index.r), index.build_seconds, len(stale_ids))
        clear_user_cache()
        _release_freed_memory()
        return index

    def status(self) -> dict[str, Any]:
        info: dict[str, Any] = {"state": self.state, "numpy": NUMPY_AVAILABLE, "default_mode": DEFAULT_MODE,
                                "last_error": self.last_error, "building": self._building}
        if not NUMPY_AVAILABLE:
            info["state"] = "unavailable (numpy missing)"
        if self.index is not None:
            info.update(self.index.summary())
        info["last_fallback"] = dict(_LAST_FALLBACK)
        return info


_RELATED_FIELDS = ("ids", "upd", "live", "ptr", "keys")


def related_cache_path() -> str:
    import os
    import tempfile

    return os.environ.get("OPENFEED_RANKER_CACHE") or os.path.join(tempfile.gettempdir(), "openfeed-ranker-related.npz")


def _save_related_cache(related: dict[str, Any]) -> None:
    """Video ids, update stamps and hashed related ids only: nothing user-specific."""
    import os

    path = related_cache_path()
    try:
        tmp = path + ".tmp.npz"
        np.savez(tmp, **{k: np.asarray(related[k]) for k in _RELATED_FIELDS})
        os.replace(tmp, path)
    except Exception as exc:  # noqa: BLE001 - the cache only speeds up the next start
        logger.info("ranker related cache not saved: %s", exc)


def _load_related_cache() -> dict[str, Any] | None:
    import os

    path = related_cache_path()
    if not os.path.exists(path):
        return None
    try:
        with np.load(path, allow_pickle=False) as data:
            related = {k: data[k] for k in _RELATED_FIELDS}
        if len(related["ptr"]) != len(related["ids"]) + 1 or (len(related["ids"]) and np.any(np.diff(related["ids"]) <= 0)):
            raise ValueError("inconsistent arrays")
        return related
    except Exception as exc:  # noqa: BLE001 - a bad cache means a full read, nothing worse
        logger.info("ranker related cache ignored: %s", exc)
        return None


INDEX = IndexManager()

_USER_CACHE: OrderedDict[str, UserRanking] = OrderedDict()
_USER_CACHE_LOCK = threading.Lock()
_USER_LOCKS: dict[str, threading.Lock] = {}


def clear_user_cache() -> None:
    with _USER_CACHE_LOCK:
        _USER_CACHE.clear()


def _user_lock(user_key: str) -> threading.Lock:
    with _USER_CACHE_LOCK:
        return _USER_LOCKS.setdefault(user_key, threading.Lock())


def status() -> dict[str, Any]:
    return INDEX.status()


def warm() -> None:
    """Start the first build in the background (called at app startup)."""
    if NUMPY_AVAILABLE:
        INDEX.maybe_refresh(force=True)


# ---- database reads -----------------------------------------------------------------------
_FINGERPRINT_SQL = """
SELECT
  (SELECT concat_ws(':', COUNT(*), MAX(id)) FROM user_video_events WHERE user_key = %(u)s) AS events,
  (SELECT concat_ws(':', COUNT(*), MAX(id), MAX(updated_at)) FROM user_video_feedback WHERE user_key = %(u)s) AS feedback,
  (SELECT concat_ws(':', COUNT(*), MAX(id), MAX(updated_at)) FROM playback_progress WHERE user_key = %(u)s) AS progress,
  (SELECT concat_ws(':', COUNT(*), md5(string_agg(COALESCE(f.youtube_channel_id, ''), ',' ORDER BY f.youtube_channel_id)))
     FROM feeds f JOIN user_subscriptions us ON us.feed_id = f.id WHERE us.user_key = %(u)s) AS subscriptions,
  (SELECT md5(profile::text) FROM users WHERE authentik_sub = %(u)s) AS profile,
  (SELECT md5(string_agg(utp.topic || ':' || utp.preference, ',' ORDER BY utp.topic))
     FROM user_topic_preferences utp JOIN users tu ON tu.id = utp.user_id WHERE tu.authentik_sub = %(u)s) AS topics,
  (SELECT concat_ws(':', COUNT(*), MAX(imported_at)) FROM takeout_watched_videos WHERE user_key = %(u)s) AS takeout
"""


def fingerprint(cur: Any, user_key: str) -> tuple[Any, ...]:
    """One cheap query whose answer changes whenever anything ranking reads for this user does,
    including their part of the catalog (catalog_scope.py): a search that adds provenance
    rows ranks again, and so does an install becoming shared."""
    from . import catalog_scope

    scoped = catalog_scope.applies(user_key)
    sql = _FINGERPRINT_SQL.rstrip() + (f",\n  {catalog_scope.FINGERPRINT_SQL} AS scope\n" if scoped else "\n")
    cur.execute(sql, {"u": user_key})
    row = cur.fetchone() or {}
    keys = ("events", "feedback", "progress", "subscriptions", "profile", "topics", "takeout")
    return (*(str(row.get(k) or "") for k in keys), f"scoped:{row.get('scope') or ''}" if scoped else "unscoped")


def load_signals(cur: Any, user_key: str) -> UserSignals:
    cur.execute("SELECT profile FROM users WHERE authentik_sub = %s", (user_key,))
    profile_row = cur.fetchone() or {}
    cur.execute(
        "SELECT DISTINCT lower(btrim(f.youtube_channel_id)) AS k FROM feeds f "
        "JOIN user_subscriptions us ON us.feed_id = f.id "
        "WHERE us.user_key = %s AND f.youtube_channel_id IS NOT NULL AND btrim(f.youtube_channel_id) <> ''",
        (user_key,),
    )
    subscriptions = [row["k"] for row in cur.fetchall()]
    cur.execute(
        """
        SELECT pp.video_id, pp.total_watch_seconds, pp.percent_completed, pp.completed,
               pp.manually_marked_watched, pp.last_watched_at,
               COALESCE(v.duration_seconds::float8, pp.duration_seconds) AS duration
        FROM playback_progress pp LEFT JOIN videos v ON v.id = pp.video_id
        WHERE pp.user_key = %s
        """,
        (user_key,),
    )
    progress = list(cur.fetchall())
    cur.execute("SELECT video_id, reaction, not_interested, updated_at FROM user_video_feedback WHERE user_key = %s", (user_key,))
    feedback = list(cur.fetchall())
    cur.execute(
        "SELECT video_id, event_type, COUNT(*) AS n, MAX(created_at) AS last_at FROM user_video_events WHERE user_key = %s GROUP BY 1, 2",
        (user_key,),
    )
    events = list(cur.fetchall())
    profile = profile_row.get("profile") if isinstance(profile_row.get("profile"), dict) else {}
    # Rotation: on how many earlier days each video was already shown here. Today's
    # impressions do not count, so the pages of one day agree with each other.
    impressions: list[dict[str, Any]] = []
    try:
        cur.execute(
            """
            SELECT video_id, COUNT(DISTINCT date_trunc('day', shown_at)) AS days
            FROM feed_impressions
            WHERE metadata->>'user_key' = %s AND video_id IS NOT NULL
              AND shown_at > now() - make_interval(days => %s)
              AND shown_at < date_trunc('day', now())
            GROUP BY video_id
            """,
            (user_key, ROTATION_WINDOW_DAYS),
        )
        impressions = list(cur.fetchall())
    except Exception as exc:  # noqa: BLE001 - rotation is optional; never fail ranking for it
        logger.info("rotation skipped: %s", exc)
        try:
            cur.connection.rollback()
        except Exception:  # noqa: BLE001
            pass
    topic_rows = _optional_rows(cur, lambda: _topic_rows(cur, user_key), "topic controls")
    takeout_rows = _optional_rows(cur, lambda: _takeout_rows(cur, user_key), "imported history")
    signals = signals_from_rows(subscriptions, progress, feedback, events, profile, impressions, topic_rows, takeout_rows)
    return with_scope(cur, user_key, signals)


def with_scope(cur: Any, user_key: str, signals: UserSignals) -> UserSignals:
    """Attach the person's part of the catalog (catalog_scope.py). Not optional: if it cannot
    be read this raises, and the caller falls back to the classic ranker, whose SQL is scoped
    the same way. A ranking over the whole catalog is never the fallback."""
    from . import catalog_scope

    if not catalog_scope.applies(user_key):
        return signals
    through, scoped, own = catalog_scope.scope_sets(cur, user_key)
    signals.scoped = True
    signals.scope_through = through
    signals.scope_ids = set(scoped)
    signals.own_ids = set(own)
    return signals


def _optional_rows(cur: Any, read: Any, what: str) -> list[dict[str, Any]]:
    """A read ranking can do without: on any error it is skipped (and logged), never fatal."""
    try:
        return list(read())
    except Exception as exc:  # noqa: BLE001
        logger.info("%s skipped: %s", what, exc)
        try:
            cur.connection.rollback()
        except Exception:  # noqa: BLE001
            pass
        return []


def topic_terms(topic: str) -> list[str]:
    """The terms a followed or muted topic must all have in a video's topic vector. Uses the
    same tokenizer that built the vectors; a topic it drops entirely (too short, a stopword) is
    matched as the literal lowercased word."""
    from . import topics

    text = str(topic or "").strip().lower()
    terms = sorted(set(topics.tokenize(text)))
    return terms or ([text] if text else [])


def _topic_rows(cur: Any, user_key: str) -> list[dict[str, Any]]:
    cur.execute(
        "SELECT utp.topic, utp.preference FROM user_topic_preferences utp JOIN users u ON u.id = utp.user_id "
        "WHERE u.authentik_sub = %s",
        (user_key,),
    )
    wanted = {"follow": [], "mute": []}
    for row in cur.fetchall():
        terms = topic_terms(row.get("topic"))
        if terms and row.get("preference") in wanted:
            wanted[row["preference"]].append(terms)
    if not wanted["follow"] and not wanted["mute"]:
        return []
    params: list[Any] = []

    def any_of(term_sets: list[list[str]]) -> str:
        if not term_sets:
            return "false"
        params.extend(term_sets)
        return "(" + " OR ".join("v.topic_vector ?& %s::text[]" for _ in term_sets) + ")"

    preferred, muted = any_of(wanted["follow"]), any_of(wanted["mute"])
    cur.execute(
        f"SELECT id, {preferred} AS preferred, {muted} AS muted FROM videos v "
        f"WHERE jsonb_typeof(v.topic_vector) = 'object' AND ({preferred} OR {muted})",
        params + params,
    )
    return list(cur.fetchall())


def _takeout_rows(cur: Any, user_key: str) -> list[dict[str, Any]]:
    cur.execute(
        "SELECT v.id, t.watched_at FROM takeout_watched_videos t JOIN videos v ON v.youtube_video_id = t.youtube_video_id "
        "WHERE t.user_key = %s",
        (user_key,),
    )
    return list(cur.fetchall())


def _topic_vectors(cur: Any) -> Any:
    def fetch(video_ids: list[int]) -> dict[int, Any]:
        if not video_ids:
            return {}
        cur.execute("SELECT id, topic_vector FROM videos WHERE id = ANY(%s)", (video_ids,))
        out: dict[int, Any] = {}
        for row in cur.fetchall():
            vector = row.get("topic_vector")
            if isinstance(vector, str):
                import json

                try:
                    vector = json.loads(vector)
                except ValueError:
                    vector = None
            out[int(row["id"])] = vector if isinstance(vector, dict) else None
        return out

    return fetch


def _row_sql() -> str:
    from . import db

    # The legacy ranking query's column list, restricted to the ids being shown, in order.
    return f"""
        WITH ids AS (SELECT t.id, t.ord FROM unnest(%(ids)s::bigint[]) WITH ORDINALITY AS t(id, ord)),
        event_rollup AS (
          SELECT video_id,
                 COUNT(*) FILTER (WHERE event_type = 'hide') AS hide_count,
                 COUNT(*) FILTER (WHERE event_type = 'mark_watched') AS watched_count,
                 COUNT(*) FILTER (WHERE event_type = 'watch_page_open') AS open_count,
                 SUM(CASE
                   WHEN event_type = 'more_like_this' THEN 5
                   WHEN event_type = 'less_like_this' THEN -6
                   WHEN event_type = 'comment_like' THEN 1
                   WHEN event_type = 'comment_dislike' THEN -1.5
                   ELSE 0
                 END) AS feedback_score
          FROM user_video_events
          WHERE user_key = %(u)s AND video_id IN (SELECT id FROM ids)
          GROUP BY video_id
        )
        SELECT v.id, v.youtube_video_id, v.title, v.url, v.thumbnail_url, v.author,
               v.published_at, v.created_at, v.duration_seconds, v.view_count, v.tags, v.topic_vector, c.title AS channel_title, c.avatar_url AS channel_avatar_url, c.youtube_channel_id,
               v.source_kind, v.discovery_source,
               {db.subscribed_channel_sql("%(u)s")} AS is_subscribed_channel,
               uvf.reaction, COALESCE(uvf.not_interested, false) AS not_interested,
               COALESCE(pp.percent_completed, 0) AS percent_completed,
               COALESCE(pp.completed, false) AS progress_completed,
               COALESCE(pp.manually_marked_watched, false) AS manually_marked_watched,
               pp.resume_seconds,
               COALESCE(er.feedback_score, 0) AS feedback_score,
               COALESCE(er.hide_count, 0) AS hide_count,
               COALESCE(er.watched_count, 0) AS watched_count,
               COALESCE(er.open_count, 0) AS open_count,
               0.0::float8 AS recommendation_score
        FROM ids
        JOIN videos v ON v.id = ids.id
        LEFT JOIN channels c ON c.id = v.channel_id
        LEFT JOIN event_rollup er ON er.video_id = v.id
        LEFT JOIN user_video_feedback uvf ON uvf.video_id = v.id AND uvf.user_key = %(u)s
        LEFT JOIN playback_progress pp ON pp.video_id = v.id AND pp.user_key = %(u)s
        ORDER BY ids.ord
    """


def _cached_entry(cur: Any, index: CorpusIndex, user_key: str) -> UserRanking:
    # The day lets rotation, decay and freshness move on without a data change; within a day
    # the ranking only changes when the user's own data or the library does.
    key = (index.version, fingerprint(cur, user_key), _MODE_OVERRIDE.get(), time.strftime("%Y-%m-%d", time.gmtime()))
    with _USER_CACHE_LOCK:
        entry = _USER_CACHE.get(user_key)
        if entry is not None and entry.key == key:
            _USER_CACHE.move_to_end(user_key)
            return entry
    with _user_lock(user_key):
        with _USER_CACHE_LOCK:
            entry = _USER_CACHE.get(user_key)
            if entry is not None and entry.key == key:
                return entry
        entry = prepare_user(index, load_signals(cur, user_key), key)
        with _USER_CACHE_LOCK:
            _USER_CACHE[user_key] = entry
            _USER_CACHE.move_to_end(user_key)
            while len(_USER_CACHE) > USER_CACHE_SIZE:
                _USER_CACHE.popitem(last=False)
        return entry


def _lane(cur: Any, index: CorpusIndex, entry: UserRanking, user_key: str, lane: str) -> LaneRanking:
    ranking = entry.lanes.get(lane)
    if ranking is not None:
        return ranking
    with _user_lock(user_key):
        ranking = entry.lanes.get(lane)
        if ranking is None:
            ranking = rank_lane(index, entry, lane, _topic_vectors(cur))
            entry.lanes[lane] = ranking
    return ranking


def home_partition(orders: dict[str, list[int]], depth: int = RANK_DEPTH) -> dict[str, list[int]]:
    """Walk Home's lane schedule and give each lane its rows in order, minus every row an
    earlier page (of any lane) already took. Rows past the simulated depth follow in lane order,
    still without rows another lane claimed."""
    out: dict[str, list[int]] = {lane: [] for lane in orders}
    ptr = {lane: 0 for lane in orders}
    claimed: set[int] = set()

    def take(lane: str, count: int) -> int:
        order, got = orders.get(lane) or [], 0
        while got < count and ptr[lane] < len(order):
            row = order[ptr[lane]]
            ptr[lane] += 1
            if row not in claimed:
                claimed.add(row)
                out[lane].append(row)
                got += 1
        return got

    for lane, count in HOME_SCHEDULE_HEAD:
        if lane in orders:
            take(lane, count)
    while len(claimed) < depth:
        if not sum(take(lane, count) for lane, count in HOME_SCHEDULE_CYCLE if lane in orders):
            break
    for lane, order in orders.items():
        mine = set(out[lane])
        out[lane] += [row for row in order[ptr[lane]:] if row not in claimed or row in mine]
    return out


def _home_lane(cur: Any, index: CorpusIndex, entry: UserRanking, user_key: str, lane: str) -> LaneRanking:
    """A lane as Home serves it: the base ranking (interleaved when tuning is on) after
    home_partition, so scrolling Home never meets the same video in two lanes."""
    key = f"{lane}@home"
    ranking = entry.lanes.get(key)
    if ranking is not None:
        return ranking
    bases = {name: _base_lane(cur, index, entry, user_key, name)[0] for name in ("recommended", *FILTERED_LANES)}
    parts = home_partition({name: base.rows.tolist() for name, base in bases.items()})
    with _user_lock(user_key):
        for name, base in bases.items():
            where = {int(r): i for i, r in enumerate(base.rows.tolist())}
            pick = np.asarray([where[r] for r in parts[name]], dtype=np.int64)
            entry.lanes[f"{name}@home"] = LaneRanking(
                rows=base.rows[pick] if len(pick) else base.rows[:0],
                scores=base.scores[pick] if len(pick) else base.scores[:0],
                source=base.source[pick] if base.source is not None and len(pick) else (base.source[:0] if base.source is not None else None),
                chan_best=base.chan_best,
                teams=base.teams[pick] if base.teams is not None and len(pick) else (base.teams[:0] if base.teams is not None else None),
            )
    return entry.lanes[key]


def _base_lane(cur: Any, index: CorpusIndex, entry: UserRanking, user_key: str, lane: str) -> tuple[LaneRanking, UserRanking]:
    """(ranking, the user view its reasons are explained with) before the Home partition."""
    if lane == "recommended" and entry.signals.explore:
        return _interleaved(cur, index, entry, user_key, lane)
    return _lane(cur, index, entry, user_key, lane), entry


def mode_view(index: CorpusIndex, entry: UserRanking, mode: str) -> UserRanking:
    """The same user ranked in another v2 mode: same signals, exclusions, rotation and controls,
    only the mode and its walk differ (blend keeps the sealed mass-mixed walk)."""
    if entry.mode == mode:
        return entry
    view = entry.views.get(mode)
    if view is None:
        g_sub = entry.g_sub if entry.g_sub is not None else np.zeros(index.n)
        g_evt = entry.g_evt if entry.g_evt is not None else np.zeros(index.n)
        graph = graph_scores(index, entry.s, entry.walk_graph) if mode == "blend" and entry.s is not None and entry.s.any() else g_sub + g_evt
        view = dataclasses.replace(entry, mode=mode, graph=graph, lanes={}, views={})
        entry.views[mode] = view
    return view


def team_draft(a: list[int], b: list[int], depth: int, seed: str) -> tuple[list[int], list[int]]:
    """Team-draft interleaving (Radlinski, Kurup and Joachims 2008).

    Each slot goes to the team with fewer picks so far, a seeded coin deciding ties, and that
    team adds its best row not already shown. Returns (rows, team per row: 0 = a, 1 = b). The
    same lists and seed always give the same page, which is what keeps one day's pages in
    agreement: the seed is the user and the day.
    """
    rng = random.Random(seed)
    lists = (a, b)
    ptr = [0, 0]
    picks = [0, 0]
    used: set[int] = set()
    rows: list[int] = []
    teams: list[int] = []
    while len(rows) < depth:
        for t in (0, 1):
            while ptr[t] < len(lists[t]) and lists[t][ptr[t]] in used:
                ptr[t] += 1
        live = [t for t in (0, 1) if ptr[t] < len(lists[t])]
        if not live:
            break
        if len(live) == 1:
            team = live[0]
        elif picks[0] != picks[1]:
            team = 0 if picks[0] < picks[1] else 1
        else:
            team = 0 if rng.random() < 0.5 else 1
        row = lists[team][ptr[team]]
        ptr[team] += 1
        used.add(row)
        rows.append(row)
        teams.append(team)
        picks[team] += 1
    return rows, teams


def _interleaved(cur: Any, index: CorpusIndex, entry: UserRanking, user_key: str, lane: str) -> tuple[LaneRanking, UserRanking]:
    """The lane as a team-draft interleave of graph (team 0) and blend (team 1), and the graph
    view its reasons are explained with (the same wording for both teams, so a card does not
    give its team away)."""
    with _user_lock(user_key):
        graph_view = mode_view(index, entry, "graph")
        blend_view = mode_view(index, entry, "blend")
    key = f"{lane}@tdi"
    ranking = entry.lanes.get(key)
    if ranking is not None:
        return ranking, graph_view
    a = _lane(cur, index, graph_view, user_key, lane)
    b = _lane(cur, index, blend_view, user_key, lane)
    day = entry.key[-1] if entry.key else time.strftime("%Y-%m-%d", time.gmtime())
    rows, teams = team_draft(a.rows.tolist(), b.rows.tolist(), RANK_DEPTH, f"{INTERLEAVE_EXPERIMENT}:{user_key}:{day}:{lane}")
    src_a = dict(zip(a.rows.tolist(), a.source.tolist())) if a.source is not None else {}
    source = [src_a.get(r, 2 if graph_view.graph[r] > 0 else -1) for r in rows]
    ranking = LaneRanking(rows=np.asarray(rows, dtype=np.int64), scores=1.0 - np.arange(len(rows)) / max(len(rows), 1),
                          source=np.asarray(source, dtype=np.int64), chan_best=a.chan_best,
                          teams=np.asarray(teams, dtype=np.int64))
    entry.lanes[key] = ranking
    return ranking, graph_view


def _serving_index() -> CorpusIndex | None:
    """The index when v2 can serve this request, else None (reason noted)."""
    if not NUMPY_AVAILABLE:
        note_fallback("numpy is not installed")
        return None
    if _MODE_OVERRIDE.get() == "classic":
        return None
    from .config import get_settings

    if not get_settings().database_url:
        note_fallback("database not configured")
        return None
    index = INDEX.current()
    if index is None:
        note_fallback("library index not built yet" if not INDEX.last_error else f"library index failed: {INDEX.last_error}")
    return index


def recommended_rows(limit: int, offset: int, lane: str, user_key: str) -> list[dict[str, Any]] | None:
    """v2 rows for a lane, or None when the legacy ranker should serve (reason noted)."""
    if lane not in SERVED_LANES:
        return None
    from . import db

    index = _serving_index()
    if index is None:
        return None
    with db.connect() as conn:
        with conn.cursor() as cur:
            entry = _cached_entry(cur, index, user_key)
            if entry.mode == "classic":
                return None
            interleave = lane == "recommended" and entry.signals.explore
            ranking, view = _base_lane(cur, index, entry, user_key, lane)
            # Past the shelf's own ranks, recommended and the filtered lanes are read as Home
            # reads them, so no video turns up in two lanes (home_partition).
            home_head = HOME_SCHEDULE_HEAD[0][1]
            if lane in FILTERED_LANES or (lane == "recommended" and offset + limit > home_head):
                ranking = _home_lane(cur, index, entry, user_key, lane)
            rows_slice = ranking.rows[offset:offset + limit]
            scores_slice = ranking.scores[offset:offset + limit]
            if not len(rows_slice):
                return []
            cur.execute(_row_sql(), {"ids": [int(i) for i in index.ids[rows_slice]], "u": user_key})
            fetched = {int(row["id"]): dict(row) for row in cur.fetchall()}
            sources = ranking.source[offset:offset + limit] if ranking.source is not None else None
            teams = ranking.teams[offset:offset + limit] if ranking.teams is not None else None
            parts = []
            for pos, row_index in enumerate(rows_slice.tolist()):
                src = int(sources[pos]) if sources is not None else (0 if view.graph is not None and view.graph[row_index] > 0 else -1)
                parts.append(explain_parts(index, view, row_index, src, ranking.chan_best) if view.mode == "graph" else ("legacy", None))
            labels = _reason_labels(parts, cur)
    now_ts = time.time()
    out: list[dict[str, Any]] = []
    model = "v2-tdi" if interleave else f"v2-{entry.mode}"
    for position, (row_index, score) in enumerate(zip(rows_slice.tolist(), scores_slice.tolist()), start=offset):
        row = fetched.get(int(index.ids[row_index]))
        if row is None:  # deleted since the index was built
            continue
        graph = float(view.graph[row_index])
        prior = float(index.prior[row_index])
        debug: dict[str, float] = {}
        if graph > 0:
            debug["graph_walk"] = round(graph, 8)
        if view.mode == "blend" or graph <= 0:
            debug["velocity_prior"] = round(prior, 4)
        if entry.demoted[row_index]:
            debug["channel_demotion"] = CHANNEL_DEMOTION
        if entry.topic_mult is not None and entry.topic_mult[row_index] != 1.0:
            debug["preferred_topic"] = PREFERRED_TOPIC_BOOST
        row["is_subscribed_channel"] = bool(row.get("is_subscribed_channel", True))
        row["recommendation_score"] = round(float(score), 6)
        row["score_debug"] = debug
        kind, ref = parts[position - offset]
        if kind == "legacy":
            row["recommendation_reason"] = explain(index, view, row_index)
        else:
            age_days = (now_ts - index.pub_ts[row_index]) / 86400.0 if index.pub_ts is not None else float("nan")
            row["recommendation_reason"] = reason_text(kind, labels.get((kind, ref)), bool(age_days <= 3))
            if sources is not None:
                row["recommendation_source"] = SOURCES[int(sources[position - offset])] if int(sources[position - offset]) >= 0 else "popular"
        row["topic_tokens"] = db._recommendation_tokens(row)[:8]
        row["ranker"] = {"model": model, "lane": lane, "rank": position, "score": round(float(score), 6),
                         "graph": graph, "prior": round(prior, 4)}
        if teams is not None:
            row["ranker"]["team"] = INTERLEAVE_TEAMS[int(teams[position - offset])]
            row["ranker"]["experiment"] = INTERLEAVE_EXPERIMENT
        out.append(row)
    return out


# ---- watch page: Up next -------------------------------------------------------------------
def up_next_order(index: CorpusIndex, entry: UserRanking, anchor: int, limit: int) -> tuple[list[int], list[str]]:
    """Rows for the watch page's Up next and what put each there.

    Three tiers, in order: the anchor's own related list (YouTube's up next), then videos its
    walk reaches (they share related-list entries with it), then the user's own walk and quality.
    Within a tier: Q x topics x demotion x (position weight UPNEXT_DIRECT_BONUS 1.0 -> 0.5 on the
    related list + the anchor walk, max-normalised) + a small share of the user's own walk to
    break near-ties. Then at most UPNEXT_CHANNEL_CAP per channel (YouTube's same-channel follow-ups
    keep up to UPNEXT_ANCHOR_CHANNEL_CAP), and the anchor creator's newest eligible uploads take
    the UPNEXT_CREATOR_SLOTS while that channel is under its cap.
    kinds: "related" (YouTube's list), "walk" (two links away), "creator", "prior".
    """
    n = index.n
    ok = entry.base_ok.copy()
    direct_bonus = np.zeros(n)
    if index.rel_ptr is not None:
        ctxs = index.rel_ctx[index.rel_ptr[anchor]:index.rel_ptr[anchor + 1]]
        length = max(len(ctxs), 1)
        for position, ctx in enumerate(ctxs.tolist()):
            owner = int(index.self_owner[ctx])
            if owner >= 0 and direct_bonus[owner] == 0:
                direct_bonus[owner] = UPNEXT_DIRECT_BONUS * (1.0 - position / (2.0 * length))
    walk_graph = entry.walk_graph
    if entry.scope is not None:
        # The video on screen and its own related list are this person's whatever the cached
        # scope says (the open is recorded as provenance, perhaps after this was cached).
        direct = direct_bonus > 0
        if not entry.scope[anchor] or not entry.scope[direct].all():
            scope = entry.scope.copy()
            scope[anchor] = True
            scope |= direct
            walk_graph = scoped_graph(index, scope)
        ok |= direct & entry.unscoped_ok
    ok[anchor] = False
    seed = np.zeros(n)
    seed[anchor] = 1.0
    walk = graph_scores(index, seed, walk_graph)
    walk = walk / max(float(walk.max()), 1e-300)
    g_sub = entry.g_sub if entry.g_sub is not None else np.zeros(n)
    g_evt = entry.g_evt if entry.g_evt is not None else np.zeros(n)
    user = (g_sub / max(float(g_sub.max()), 1e-300) + g_evt / max(float(g_evt.max()), 1e-300)) / 2.0
    mult = entry.feel_mult if entry.feel_mult is not None else np.ones(n)
    if entry.topic_mult is not None:
        mult = mult * entry.topic_mult
    if entry.demoted is not None:
        mult = np.where(entry.demoted, mult * CHANNEL_DEMOTION, mult)
    tier = np.where(direct_bonus > 0, 20.0, np.where(walk > 0, 10.0, 0.0))
    score = tier + (walk + direct_bonus) * mult + UPNEXT_USER_WEIGHT * user
    reached = ok & ((walk > 0) | (direct_bonus > 0))
    pool = max(limit * 6, 60)
    candidates = _ordered(score, reached, pool)
    if len(candidates) < pool:
        pct = index.prior_pct if entry.prior_pct is None else entry.prior_pct
        candidates += _ordered(user * mult + 1e-3 * pct, ok & ~reached, pool - len(candidates))
    anchor_chan = int(index.chan[anchor])
    picks: list[int] = []
    deferred: list[int] = []
    per_channel: dict[int, int] = {}
    for row in candidates:
        ch = int(index.chan[row])
        cap = UPNEXT_ANCHOR_CHANNEL_CAP if ch == anchor_chan else UPNEXT_CHANNEL_CAP
        if per_channel.get(ch, 0) >= cap:
            deferred.append(row)
            continue
        per_channel[ch] = per_channel.get(ch, 0) + 1
        picks.append(row)
        if len(picks) >= limit:
            break
    if len(picks) < limit:
        picks += deferred[: limit - len(picks)]
    # The creator's latest, YouTube-style: a couple of slots near the top, unless the related
    # list or the walk already placed them (then they keep that place).
    own = index.channel_rows(index.chan_keys[anchor_chan]) if index.chan_keys[anchor_chan] else np.zeros(0, dtype=np.int64)
    own = own[ok[own]] if len(own) else own
    placed = {r for r in picks if reached[r]}
    room = max(0, UPNEXT_ANCHOR_CHANNEL_CAP - sum(1 for r in placed if int(index.chan[r]) == anchor_chan))
    newest: list[int] = []
    if len(own) and room and index.pub_ts is not None:
        dated = own[~np.isnan(index.pub_ts[own])]
        for row in dated[np.argsort(-index.pub_ts[dated], kind="stable")].tolist():
            if len(newest) >= min(room, len(UPNEXT_CREATOR_SLOTS)):
                break
            if row not in placed:
                newest.append(row)
    creator = set(newest)
    picks = [r for r in picks if r not in creator]
    for slot, row in zip(UPNEXT_CREATOR_SLOTS, newest):
        picks.insert(min(slot, len(picks)), row)
    picks = picks[:limit]
    kinds = ["creator" if r in creator else "related" if direct_bonus[r] > 0 else "walk" if walk[r] > 0 else "prior" for r in picks]
    return picks, kinds


UPNEXT_REASONS = {
    "related": "Related to this video",
    "walk": "Often listed with this video",
    "creator": "Latest from this channel",
    "prior": "Picked for you",
}


def up_next_rows(anchor_video_id: int, limit: int, user_key: str) -> list[dict[str, Any]] | None:
    """Up next for the watch page from the anchor's own related graph, or None when the
    legacy ordering should serve (index not ready, the anchor not indexed yet, classic mode)."""
    from . import db

    index = _serving_index()
    if index is None:
        return None
    anchor_rows = index.rows_for_ids([int(anchor_video_id)])
    if not len(anchor_rows):
        return None
    with db.connect() as conn:
        with conn.cursor() as cur:
            entry = _cached_entry(cur, index, user_key)
            if entry.mode == "classic":
                return None
            rows, kinds = up_next_order(index, entry, int(anchor_rows[0]), max(1, int(limit)))
            if not rows:
                return []
            cur.execute(_row_sql(), {"ids": [int(i) for i in index.ids[rows]], "u": user_key})
            fetched = {int(row["id"]): dict(row) for row in cur.fetchall()}
    out: list[dict[str, Any]] = []
    for position, (row_index, kind) in enumerate(zip(rows, kinds)):
        row = fetched.get(int(index.ids[row_index]))
        if row is None:
            continue
        row["is_subscribed_channel"] = bool(row.get("is_subscribed_channel", True))
        row["recommendation_reason"] = UPNEXT_REASONS[kind]
        row["recommendation_source"] = kind
        row["ranker"] = {"model": "v2-upnext", "lane": "up-next", "rank": position}
        out.append(row)
    return out


def _reason_labels(parts: list[tuple[str, Any]], cur: Any) -> dict[tuple[str, Any], str]:
    """Titles for "watched" video ids and names for "follow" channel keys: one query, on the
    request's own cursor."""
    video_ids = sorted({ref for kind, ref in parts if kind == "watched" and ref is not None})
    channel_keys = sorted({ref for kind, ref in parts if kind in ("follow", "similar") and ref})
    labels: dict[tuple[str, Any], str] = {}
    if not video_ids and not channel_keys:
        return labels
    try:
        cur.execute(
            """
            SELECT 'watched' AS kind, id::text AS ref, title FROM videos WHERE id = ANY(%(v)s)
            UNION ALL
            SELECT DISTINCT ON (lower(youtube_channel_id)) 'follow', lower(youtube_channel_id), title
            FROM channels WHERE lower(youtube_channel_id) = ANY(%(c)s) AND title IS NOT NULL
            """,
            {"v": video_ids, "c": channel_keys},
        )
        for r in cur.fetchall():
            if r["kind"] == "watched":
                labels[("watched", int(r["ref"]))] = str(r.get("title") or "")
            else:
                labels[("follow", r["ref"])] = labels[("similar", r["ref"])] = str(r.get("title") or "")
    except Exception as exc:  # noqa: BLE001 - a generic reason is fine
        logger.info("reason labels skipped: %s", exc)
    return labels


# ---- impressions ----------------------------------------------------------------------------
_IMPRESSIONS: queue.Queue[tuple[Any, ...]] = queue.Queue(maxsize=5000)
_IMPRESSION_THREAD: threading.Thread | None = None
_IMPRESSION_LOCK = threading.Lock()


def log_impressions(records: list[tuple[Any, ...]]) -> None:
    """Queue (video_id, youtube_video_id, lane, position, model_version, metadata) rows.

    Written by one background thread in batches. A full queue drops rows rather than
    blocking a page; nothing here can fail a request.
    """
    global _IMPRESSION_THREAD
    if not records:
        return
    for record in records:
        try:
            _IMPRESSIONS.put_nowait(record)
        except queue.Full:
            break
    with _IMPRESSION_LOCK:
        if _IMPRESSION_THREAD is None or not _IMPRESSION_THREAD.is_alive():
            _IMPRESSION_THREAD = threading.Thread(target=_impression_writer, name="feed-impressions", daemon=True)
            _IMPRESSION_THREAD.start()


def _impression_writer() -> None:
    import json

    from . import db

    while True:
        first = _IMPRESSIONS.get()
        batch = [first]
        while len(batch) < 500:
            try:
                batch.append(_IMPRESSIONS.get_nowait())
            except queue.Empty:
                break
        try:
            with db.connect() as conn:
                with conn.cursor() as cur:
                    cur.executemany(
                        """
                        INSERT INTO feed_impressions (user_id, video_id, youtube_video_id, surface, lane, position, model_version, metadata)
                        VALUES (NULL, %s, %s, 'home', %s, %s, %s, %s::jsonb)
                        """,
                        [(vid, yid, lane, pos, model, json.dumps(meta)) for vid, yid, lane, pos, model, meta in batch],
                    )
                conn.commit()
        except Exception as exc:  # noqa: BLE001 - logging must never take anything down
            logger.info("feed impressions not written (%s rows): %s", len(batch), exc)


def impression_records(sections: list[dict[str, Any]], user_key: str, page: int) -> list[tuple[Any, ...]]:
    """The v2 rows actually shown in recommendation shelves of a home load."""
    records: list[tuple[Any, ...]] = []
    for section in sections:
        if section.get("kind") != "recommended":
            continue
        for position, video in enumerate(section.get("videos") or [], start=1):
            meta = video.get("ranker") if isinstance(video, dict) else None
            if not isinstance(meta, dict) or not str(meta.get("model", "")).startswith("v2-"):
                continue
            metadata = {"score": meta.get("score"), "graph": meta.get("graph"), "prior": meta.get("prior"),
                        "user_key": user_key, "section": section.get("id"), "page": page, "rank": meta.get("rank")}
            if meta.get("team"):
                metadata["team"] = meta["team"]
                metadata["experiment"] = meta.get("experiment") or INTERLEAVE_EXPERIMENT
            records.append((
                video.get("id"),
                str(video.get("youtube_video_id") or ""),
                meta.get("lane"),
                position,
                meta.get("model"),
                metadata,
            ))
    return records


# ---- the online comparison: clicks on interleaved shelves -----------------------------------
def record_interleave_click(user_key: str, youtube_video_id: str) -> bool:
    """Attribute a watch-page open to the interleaving team that showed the video.

    Marks the user's most recent interleaved impression of that video within
    INTERLEAVE_CLICK_WINDOW_HOURS, once. Nothing happens for a video no interleaved shelf showed
    (search, a channel page, a link), so only shelf exposure is credited. Never raises.
    """
    from . import db

    if not youtube_video_id:
        return False
    try:
        with db.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE feed_impressions
                    SET metadata = metadata || jsonb_build_object('clicked_at', now()::text)
                    WHERE id = (
                      SELECT id FROM feed_impressions
                      WHERE youtube_video_id = %s AND metadata->>'user_key' = %s AND metadata ? 'team'
                        AND shown_at > now() - make_interval(hours => %s)
                      ORDER BY shown_at DESC LIMIT 1)
                      AND NOT metadata ? 'clicked_at'
                    """,
                    (youtube_video_id, user_key, INTERLEAVE_CLICK_WINDOW_HOURS),
                )
                changed = cur.rowcount
            conn.commit()
        return bool(changed and changed > 0)
    except Exception as exc:  # noqa: BLE001 - attribution must never fail a page
        logger.info("interleave click not recorded: %s", exc)
        return False


def wilson_interval(successes: int, trials: int, z: float = 1.96) -> tuple[float, float]:
    if trials <= 0:
        return 0.0, 1.0
    p = successes / trials
    denom = 1 + z * z / trials
    centre = (p + z * z / (2 * trials)) / denom
    half = z * math.sqrt(p * (1 - p) / trials + z * z / (4 * trials * trials)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def summarize_interleave(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Team-draft readout from (user_key, day, youtube_video_id, team, clicked) rows, one per
    distinct video a team showed a user on a day.

    A user-day is one comparison: the team with more clicked videos wins it, equal clicks is a
    tie (no clicks at all is no comparison). The preference share is blend wins over decided
    user-days, with a Wilson 95% interval.
    """
    teams = INTERLEAVE_TEAMS
    shown = {t: 0 for t in teams}
    clicks = {t: 0 for t in teams}
    per_day: dict[tuple[str, str], dict[str, int]] = {}
    for row in rows:
        team = str(row.get("team") or "")
        if team not in shown:
            continue
        shown[team] += 1
        if row.get("clicked"):
            clicks[team] += 1
            day = per_day.setdefault((str(row.get("user_key") or ""), str(row.get("day") or "")), {t: 0 for t in teams})
            day[team] += 1
    wins = {t: 0 for t in teams}
    ties = 0
    for counts in per_day.values():
        a, b = counts[teams[0]], counts[teams[1]]
        if a == b:
            ties += 1
        else:
            wins[teams[0] if a > b else teams[1]] += 1
    decided = wins[teams[0]] + wins[teams[1]]
    low, high = wilson_interval(wins[teams[1]], decided)
    return {
        "experiment": INTERLEAVE_EXPERIMENT,
        "impressions": shown,
        "clicks": clicks,
        "days_compared": len(per_day),
        "wins": wins,
        "ties": ties,
        "blend_share": (wins[teams[1]] / decided) if decided else None,
        "blend_share_ci": (low, high) if decided else None,
    }


def interleave_summary(days: int = 30) -> dict[str, Any]:
    """The readout over the last `days`, for the status page. Empty when nothing was logged."""
    from . import db

    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT metadata->>'user_key' AS user_key, date_trunc('day', shown_at)::date::text AS day,
                       youtube_video_id, metadata->>'team' AS team, bool_or(metadata ? 'clicked_at') AS clicked
                FROM feed_impressions
                WHERE metadata->>'experiment' = %s AND shown_at > now() - make_interval(days => %s)
                GROUP BY 1, 2, 3, 4
                """,
                (INTERLEAVE_EXPERIMENT, int(days)),
            )
            rows = list(cur.fetchall())
    return summarize_interleave(rows)


# ---- leave-one-subscribed-channel-out evaluation --------------------------------------------
def _dcg_weights(k: int) -> Any:
    return 1.0 / np.log2(np.arange(2, k + 2))


def loco_metrics(order_positive: Any, n_candidates: int, n_positive: int, k_ndcg: int = 20, k_recall: int = 100) -> dict[str, float]:
    """Binary nDCG@k and recall@k for one ranked list, plus the uniform-random expectation."""
    disc = _dcg_weights(k_ndcg)
    top = np.asarray(order_positive[:k_ndcg], dtype=bool)
    dcg = float((disc[: len(top)] * top).sum())
    ideal = float(disc[: min(n_positive, k_ndcg)].sum())
    recall = float(np.asarray(order_positive[:k_recall], dtype=bool).sum()) / max(n_positive, 1)
    p = n_positive / max(n_candidates, 1)
    random_dcg = float(disc[: min(k_ndcg, n_candidates)].sum()) * p
    return {
        "ndcg@20": dcg / ideal if ideal else 0.0,
        "recall@100": recall,
        "random_ndcg@20": random_dcg / ideal if ideal else 0.0,
        "random_recall@100": min(k_recall, n_candidates) / max(n_candidates, 1),
    }


def loco_evaluate(
    index: CorpusIndex,
    subscribed_keys: Iterable[str],
    modes: Iterable[str] = ("graph", "blend"),
    min_videos: int = 5,
    max_channels: int = 0,
    classic_ranked_ids: list[int] | None = None,
    feel: dict[str, float] | None = None,
    age_matched: bool = False,
    now_ts: float | None = None,
    controls: FeedControls = DEFAULT_CONTROLS,
) -> dict[str, Any]:
    """Leave one subscribed channel out, rank the whole library, see where its videos land.

    For every subscribed channel with at least `min_videos` eligible videos: drop it from the
    seeds, seed from the remaining subscriptions only (no personal events, so nothing leaks),
    and rank. Candidates are every non-subscribed eligible video plus the held-out channel's
    videos, which are the positives. Live streams, Shorts and dead videos are excluded on
    both sides, as on the lanes. `feel` is the graph-mode freshness/quality setting ({} = off,
    None = the shipped FEEL). `age_matched` keeps only negatives published in the held-out
    channel's weeks (and drops undated videos), so freshness itself earns nothing. `controls` are the
    graph-mode feed controls (default: the measured mix). `classic_ranked_ids` is the legacy ranker's list, scored against the same
    candidates; it cannot drop the channel from its own inputs, so it is not leave-one-out.
    """
    keys = sorted({str(k).strip().lower() for k in subscribed_keys if str(k or "").strip()})
    sub_mask = np.zeros(index.n, dtype=bool)
    for key in keys:
        sub_mask[index.channel_rows(key)] = True
    modes = [m for m in modes if m in ("graph", "blend")]
    classic_rows = index.rows_for_ids(classic_ranked_ids or []) if classic_ranked_ids is not None else None
    usable = ~index.blocked
    if index.dead is not None:
        usable = usable & ~index.dead
    mult = feel_multiplier(index, now_ts, feel)
    week = np.floor(index.pub_ts / (7 * 86400.0)) if index.pub_ts is not None else None
    per_channel: list[dict[str, Any]] = []
    skipped = 0
    for key in keys:
        held = index.channel_rows(key)
        held = held[usable[held]]
        if age_matched and week is not None:
            held = held[~np.isnan(week[held])]
        if len(held) < min_videos:
            skipped += 1
            continue
        if max_channels and len(per_channel) >= max_channels:
            break
        positive = np.zeros(index.n, dtype=bool)
        positive[held] = True
        candidates = (~sub_mask | positive) & usable
        if age_matched and week is not None:
            candidates &= np.isin(week, np.unique(week[held]))
        n_candidates = int(candidates.sum())
        s = seed_vector(index, [k for k in keys if k != key], {})
        graph = graph_scores(index, s) if s.any() else np.zeros(index.n)
        result: dict[str, Any] = {"channel": key, "positives": int(len(held)), "candidates": n_candidates}
        for mode in modes:
            if mode == "graph":
                # What ships: the slot mix, with no events (leave-one-out seeds subscriptions only).
                order, _ = graph_mix(index, graph, np.zeros(index.n), candidates, mult, 0.0, now_ts, depth=100, controls=controls)
            else:
                f = np.where(candidates, mode_scores(mode, graph, index.prior, candidates), -np.inf)
                rows = np.flatnonzero(candidates)
                order = rows[np.lexsort((rows, -f[rows]))]
            result[mode] = loco_metrics(positive[order[:100]], n_candidates, len(held))
        if classic_rows is not None:
            ranked = classic_rows[candidates[classic_rows]]
            result["classic"] = loco_metrics(positive[ranked[:100]], n_candidates, len(held))
        per_channel.append(result)
    summary: dict[str, Any] = {}
    for mode in [*modes, *(["classic"] if classic_rows is not None else [])]:
        values = [row[mode] for row in per_channel if mode in row]
        if not values:
            continue
        summary[mode] = {name: round(float(np.mean([v[name] for v in values])), 4) for name in values[0]}
        summary[mode]["units_with_hit_in_top20"] = int(sum(1 for v in values if v["ndcg@20"] > 0))
    return {"channels": per_channel, "summary": summary, "units": len(per_channel), "skipped_small": skipped}
