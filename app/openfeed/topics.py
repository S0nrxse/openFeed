"""Content-based similarity for ranking.

The ranker's dominant signal was overlap between a video's raw YouTube tags and the tags
of subscribed channels. That measure looked semantic and was not: the corpus carries ~4600
distinct tags across ~400 tagged videos, most of them channel names, one-offs and
misspellings (`kurzgesagt`, `kurzgesatg`, `kurzgsagt` are three different tags). Literal
string overlap on that fires on almost everything and therefore ranks almost nothing.

This replaces it with TF-IDF over the title, description, tags and channel name. Terms
that appear everywhere ("video", "channel", "subscribe") are worth little; terms that
identify a subject are worth a lot, and that falls out of the maths rather than needing a
stopword list to be complete.

Vectors are computed once at enrichment time and stored on the row, because doing it per
request would mean pulling every description into memory on every page load. Only the top
terms are kept - the tail of a TF-IDF vector is noise and storing it costs more than it
returns.
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
from collections import Counter
from typing import Any, Iterable

from . import db

logger = logging.getLogger("openfeed.topics")

TERM_RE = re.compile(r"[a-z][a-z0-9'+-]{2,}")
VECTOR_TERMS = 24
_MIN_DOCUMENT_FREQUENCY = 2

# Words that carry no subject information on YouTube specifically. TF-IDF suppresses most
# common English on its own; these are the ones that survive it because they genuinely are
# everywhere in this corpus and would otherwise look distinctive inside one channel.
STOPWORDS = frozenset("""
about above after again against all also am an and any are aren't as at be because been
before being below between both but by can cannot could couldn't did didn't do does
doesn't doing don't down during each few for from further had hadn't has hasn't have
haven't having he her here hers herself him himself his how i'm if in into is isn't it
it's its itself just let's me more most mustn't my myself no nor not of off on once only
or other ought our ours ourselves out over own same shan't she should shouldn't so some
such than that the their theirs them themselves then there these they this those through
to too under until up very was wasn't we were weren't what when where which while who
whom why with won't would wouldn't you your yours yourself yourselves
video videos watch watching subscribe subscribed subscribing channel channels like likes
comment comments share shares click links link please thanks thank support patreon merch
instagram twitter tiktok facebook discord podcast episode new latest full official
free best top get make made using use how why what one two three
""".split())


def tokenize(*parts: Any) -> list[str]:
    """Lowercase word tokens from any mix of strings and string lists."""
    tokens: list[str] = []
    for part in parts:
        if part is None:
            continue
        if isinstance(part, (list, tuple, set)):
            text = " ".join(str(item) for item in part)
        else:
            text = str(part)
        for match in TERM_RE.finditer(text.lower()):
            term = match.group(0).strip("'-+")
            if len(term) >= 3 and term not in STOPWORDS and not term.isdigit():
                tokens.append(term)
    return tokens


def document_terms(row: dict[str, Any]) -> list[str]:
    """The bag of words for one video.

    Tags and the title are repeated because a term the uploader chose as a tag or put in
    the title is a stronger claim about the subject than one buried in a description.
    """
    title = str(row.get("title") or "")
    description = str(row.get("summary") or row.get("description") or "")
    tags = row.get("tags") or []
    channel = str(row.get("channel_title") or row.get("author") or "")
    return (
        tokenize(title) * 3
        + tokenize(tags) * 2
        + tokenize(channel)
        + tokenize(_description_prose(description))
    )


_URLISH = re.compile(r"https?://|www\.|\.com|\.net|\.org|@[a-z0-9_]|#[a-z]", re.I)


def _description_prose(description: str) -> str:
    """The part of a description that is about the video.

    Merch links, affiliate blocks and social handles are promotional boilerplate, and they
    poison the profile in a way IDF cannot catch: "amazon" and "store" appear in every
    upload from one channel but are rare across the corpus, so IDF scores them as highly
    distinctive subject terms. Dropping any line carrying a URL or a handle removes almost
    all of it, and those lines contain no subject information anyway.
    """
    lines = [line.strip() for line in (description or "").splitlines()]
    prose = [line for line in lines if line and not _URLISH.search(line)]
    return " ".join(prose)[:800]


# --- corpus statistics ----------------------------------------------------------------
# Rebuilt on a timer rather than per call: IDF only shifts as the library grows, and
# recomputing it for every ranked page would dominate the request.
_IDF: dict[str, float] = {}
_IDF_EXPIRES = 0.0
_IDF_LOCK = threading.Lock()
_IDF_TTL = 900.0


def inverse_document_frequencies(force: bool = False) -> dict[str, float]:
    global _IDF, _IDF_EXPIRES
    with _IDF_LOCK:
        if not force and _IDF and time.time() < _IDF_EXPIRES:
            return _IDF
    counts, total = db.term_document_counts()
    if not total:
        return {}
    idf = {
        term: math.log((1 + total) / (1 + count)) + 1.0
        for term, count in counts.items()
        # A term seen once cannot relate two videos, and there are thousands of them.
        if count >= _MIN_DOCUMENT_FREQUENCY
    }
    with _IDF_LOCK:
        _IDF = idf
        _IDF_EXPIRES = time.time() + _IDF_TTL
    return idf


def clear_cache() -> None:
    global _IDF, _IDF_EXPIRES
    with _IDF_LOCK:
        _IDF = {}
        _IDF_EXPIRES = 0.0


def compute_vector(row: dict[str, Any], idf: dict[str, float] | None = None) -> dict[str, float]:
    """An L2-normalised TF-IDF vector, truncated to its strongest terms.

    Normalising means a long description cannot outweigh a short one purely by length,
    which matters because cosine similarity is the whole point.
    """
    idf = idf if idf is not None else inverse_document_frequencies()
    counts = Counter(document_terms(row))
    if not counts:
        return {}
    weights: dict[str, float] = {}
    for term, count in counts.items():
        weight = idf.get(term)
        if weight is None:
            continue
        # Sublinear term frequency: the tenth mention of a word says little more than the
        # third, and raw counts let a repeated tag dominate a vector.
        weights[term] = (1.0 + math.log(count)) * weight
    if not weights:
        return {}
    top = dict(sorted(weights.items(), key=lambda item: item[1], reverse=True)[:VECTOR_TERMS])
    norm = math.sqrt(sum(value * value for value in top.values()))
    if not norm:
        return {}
    return {term: round(value / norm, 5) for term, value in top.items()}


def cosine(left: dict[str, float], right: dict[str, float]) -> float:
    """Both sides are already normalised, so the dot product is the cosine."""
    if not left or not right:
        return 0.0
    # Iterate the smaller side; vectors are sparse and this is on the ranking hot path.
    if len(left) > len(right):
        left, right = right, left
    return sum(weight * right.get(term, 0.0) for term, weight in left.items())


def rebuild_vectors(limit: int = 500, force: bool = False) -> dict[str, Any]:
    """Compute and store vectors for videos that lack one."""
    idf = inverse_document_frequencies(force=True)
    if not idf:
        return {"ok": False, "reason": "empty_corpus", "updated": 0}
    rows = db.videos_needing_topic_vectors(limit, force)
    updates: list[tuple[int, str]] = []
    for row in rows:
        vector = compute_vector(row, idf)
        if vector:
            updates.append((int(row["id"]), json.dumps(vector)))
    stored = db.store_topic_vectors(updates)
    return {"ok": True, "candidates": len(rows), "updated": stored, "vocabulary": len(idf)}


def _parse_vector(value: Any) -> dict[str, float] | None:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    if not isinstance(value, dict) or not value:
        return None
    cleaned = {str(term): float(weight) for term, weight in value.items() if str(term).strip()}
    return cleaned or None


def profile_vector(user_key: str = "local", limit: int = 200) -> dict[str, float]:
    """What this user's viewing looks like, as one vector.

    A watch-time-weighted centroid of what they actually watched, plus explicit
    endorsements: a video watched to the end pulls the profile toward its subject
    far harder than one abandoned after a few seconds, and a like / more-like-this
    pulls harder still — it is a stated preference, not just attention held.
    """
    try:
        rows = db.watched_topic_vectors(user_key, limit)
    except Exception:
        rows = []
    try:
        liked_rows = db.liked_topic_vectors(user_key, 100)
    except Exception:
        liked_rows = []
    if not rows and not liked_rows:
        return {}
    total: dict[str, float] = {}
    for row in rows:
        vector = _parse_vector(row.get("topic_vector"))
        if not vector:
            continue
        seconds = float(row.get("total_watch_seconds") or 0)
        completed = bool(row.get("completed"))
        # Diminishing returns on time: an hour-long watch is a stronger signal than a
        # five-minute one, but not twelve times stronger.
        weight = math.log1p(max(seconds, 0.0) / 60.0) + (0.75 if completed else 0.0)
        # A subject you were into last spring should not steer today's feed as hard
        # as one you watched this week.
        weight *= db.recency_weight(row.get("last_watched_at"))
        if weight <= 0:
            continue
        for term, value in vector.items():
            total[term] = total.get(term, 0.0) + float(value) * weight
    for row in liked_rows:
        vector = _parse_vector(row.get("topic_vector"))
        if not vector:
            continue
        # Explicit endorsement outweighs passive watching: roughly an hour of watch
        # time, decaying with age like everything else. More-like-this counts extra.
        weight = 2.2 + (0.8 * float(row.get("more_like_count") or 0))
        weight *= db.recency_weight(row.get("endorsed_at") or row.get("updated_at"))
        if weight <= 0:
            continue
        for term, value in vector.items():
            total[term] = total.get(term, 0.0) + float(value) * weight
    if not total:
        return {}
    top = dict(sorted(total.items(), key=lambda item: item[1], reverse=True)[:VECTOR_TERMS * 4])
    norm = math.sqrt(sum(value * value for value in top.values()))
    if not norm:
        return {}
    return {term: value / norm for term, value in top.items()}


def interest_exemplars(user_key: str = "local", limit: int = 30) -> list[dict[str, float]]:
    """Recent concrete interests, kept separate instead of averaged.

    The centroid above answers "what does this user like on average" — which for a
    user into tech, cooking and music is a point in the middle matching none of
    them. These exemplars answer "what are some specific things they loved": the
    newest liked videos first, then the heaviest recent watches. Scoring takes the
    max over them, so each interest survives on its own.
    """
    exemplars: list[dict[str, float]] = []
    try:
        liked_rows = db.liked_topic_vectors(user_key, min(limit, 20))
    except Exception:
        liked_rows = []
    for row in liked_rows:
        vector = _parse_vector(row.get("topic_vector"))
        if vector and vector not in exemplars:
            exemplars.append(vector)
        if len(exemplars) >= limit:
            return exemplars
    try:
        watched_rows = db.watched_topic_vectors(user_key, 60)
    except Exception:
        watched_rows = []
    scored: list[tuple[float, dict[str, float]]] = []
    for row in watched_rows:
        vector = _parse_vector(row.get("topic_vector"))
        if not vector or vector in exemplars:
            continue
        seconds = float(row.get("total_watch_seconds") or 0)
        weight = (math.log1p(max(seconds, 0.0) / 60.0) + (0.75 if row.get("completed") else 0.0)) * db.recency_weight(
            row.get("last_watched_at")
        )
        if weight > 0:
            scored.append((weight, vector))
    scored.sort(key=lambda item: item[0], reverse=True)
    for _, vector in scored:
        exemplars.append(vector)
        if len(exemplars) >= limit:
            break
    return exemplars


def explain(video_vector: dict[str, float], profile: dict[str, float], top: int = 4) -> list[str]:
    """The terms that drove a match, for the score breakdown and for debugging."""
    if not video_vector or not profile:
        return []
    shared = [(term, weight * profile.get(term, 0.0)) for term, weight in video_vector.items() if term in profile]
    shared.sort(key=lambda item: item[1], reverse=True)
    return [term for term, _ in shared[:top]]


__all__ = [
    "clear_cache",
    "compute_vector",
    "cosine",
    "document_terms",
    "explain",
    "interest_exemplars",
    "inverse_document_frequencies",
    "profile_vector",
    "rebuild_vectors",
    "tokenize",
]
