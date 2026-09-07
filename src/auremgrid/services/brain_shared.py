"""Shared, dependency-light helpers for the brain service modules."""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from typing import Any

from auremgrid.domain.errors import ValidationError

HIGH_CONFIDENCE_THRESHOLD = 0.95  # non-human claim strength; only promotion/conflict resolution reach verified


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _temporal_read_moment(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError("as_of must include a timezone")
    return value.astimezone(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def normalize_text(value: str) -> str:
    return " ".join(value.lower().split())


def _freshness_descriptor(
    observed_at: datetime | None,
    recorded_at: datetime | None,
    as_of: datetime,
) -> dict[str, Any]:
    """Explain the recency signal used by hybrid retrieval.

    Eligibility is still governed by bitemporal filters and knowledge-state
    events. This descriptor is only a bounded, user-visible explanation of
    why an otherwise eligible item received its recency contribution.
    """
    watermark = as_of if as_of.tzinfo is not None else as_of.replace(tzinfo=timezone.utc)

    def age_days(stamp: datetime | None) -> float | None:
        if stamp is None:
            return None
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return round(max(0.0, (watermark - stamp.astimezone(timezone.utc)).total_seconds() / 86400.0), 3)

    observed_age = age_days(observed_at)
    recorded_age = age_days(recorded_at)
    basis = observed_age if observed_age is not None else recorded_age
    status = "unknown" if basis is None else "fresh" if basis <= 30 else "aging" if basis <= 180 else "historical"
    # Match HybridRanker's half-life while keeping the explanation stable and
    # independent of private ranking implementation details.
    import math
    observed_score = 0.5 ** (observed_age / 180.0) if observed_age is not None else 0.5
    recorded_score = 0.5 ** (recorded_age / 180.0) if recorded_age is not None else 0.5
    return {
        "status": status,
        "observed_age_days": observed_age,
        "recorded_age_days": recorded_age,
        "recency_score": round((0.7 * observed_score) + (0.3 * recorded_score), 6),
        "watermark": watermark.astimezone(timezone.utc).isoformat(),
        "method": "observed_70_recorded_30_half_life_180d",
    }


MAX_SEARCH_LIMIT = 64


MAX_SEARCH_QUERY_CHARS = 2000


STOPWORDS = {
    "and",
    "the",
    "for",
    "with",
    "from",
    "that",
    "this",
    "into",
    "only",
}


def _token_overlap(query: str, haystack: str) -> bool:
    query_tokens = {
        token for token in query.split() if len(token) > 2 and token not in STOPWORDS
    }
    if not query_tokens:
        return query in haystack
    return any(token in haystack for token in query_tokens)


def _best_span(content: str, query: str) -> str:
    tokens = [token for token in normalize_text(query).split() if token]
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    for line in lines:
        lowered = line.lower()
        if any(token in lowered for token in tokens):
            return line[:240]
    return (lines[0] if lines else content)[:240]
