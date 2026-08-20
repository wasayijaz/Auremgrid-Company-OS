from __future__ import annotations

import math
import re
import hashlib
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable


TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_-]+")
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
    "not",
}


def tokens(text: str) -> list[str]:
    return [token for token in TOKEN_RE.findall(text.lower()) if token not in STOPWORDS]


def hashed_embedding(text: str, dims: int = 64) -> tuple[float, ...]:
    """Deterministic lexical embedding. No model, no network, no API key."""
    vector = [0.0] * dims
    counts = Counter(tokens(text))
    if not counts:
        return tuple(vector)
    for token, count in counts.items():
        # Python's hash is process-randomized; SHA-256 keeps the offline fallback
        # deterministic across restarts and projection rebuilds.
        index = int.from_bytes(hashlib.sha256(token.encode("utf-8")).digest()[:8], "big") % dims
        vector[index] += float(count)
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return tuple(value / norm for value in vector)


def cosine(left: Iterable[float], right: Iterable[float]) -> float:
    return sum(a * b for a, b in zip(left, right))


@dataclass(frozen=True)
class RankedHit:
    kind: str
    key: str
    score: float
    channels: tuple[str, ...]
    trust_level: str | None = None
    observed_at: datetime | None = None
    recorded_at: datetime | None = None
    score_components: tuple[tuple[str, float], ...] = ()


class HybridRanker:
    """Fuse eligible retrieval signals with bounded authority and recency.

    Callers are responsible for ACL and temporal eligibility.  This class only
    ranks that already-safe candidate set and never creates candidates itself.
    """

    RELEVANCE_WEIGHT = 0.75
    AUTHORITY_WEIGHT = 0.15
    RECENCY_WEIGHT = 0.10
    RECENCY_HALF_LIFE_DAYS = 180.0

    # Source trust is an explicit, canonical input. Unknown values remain
    # neutral rather than being guessed from a locator, title, or provider.
    TRUST_SCORES = {
        "internal": 0.8,
        "external": 0.6,
    }

    @staticmethod
    def _bounded(value: float) -> float:
        return max(0.0, min(1.0, float(value)))

    def _authority(self, trust_level: str | None) -> float:
        return self.TRUST_SCORES.get((trust_level or "").strip().lower(), 0.5)

    def _recency(self, observed_at: datetime | None, recorded_at: datetime | None, as_of: datetime) -> float:
        def freshness(timestamp: datetime | None) -> float:
            if timestamp is None:
                return 0.5
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            age_days = max(0.0, (as_of - timestamp.astimezone(timezone.utc)).total_seconds() / 86400.0)
            return math.pow(0.5, age_days / self.RECENCY_HALF_LIFE_DAYS)

        # Observed time represents the evidence itself; recorded time provides
        # a smaller freshness signal for late-arriving evidence.
        return (0.7 * freshness(observed_at)) + (0.3 * freshness(recorded_at))

    def fuse(self, hits: list[RankedHit], limit: int = 8, as_of: datetime | None = None) -> list[RankedHit]:
        watermark = as_of or datetime.now(timezone.utc)
        if watermark.tzinfo is None:
            watermark = watermark.replace(tzinfo=timezone.utc)
        grouped: dict[tuple[str, str], list[RankedHit]] = {}
        for hit in hits:
            key = (hit.kind, hit.key)
            grouped.setdefault(key, []).append(hit)

        merged: list[RankedHit] = []
        for (kind, key), candidates in grouped.items():
            first = candidates[0]
            # Probabilistic-OR rewards corroboration while keeping relevance in
            # [0, 1]. Each retrieval channel remains an independent input.
            miss_probability = 1.0
            channel_scores: dict[str, float] = {}
            for candidate in candidates:
                for channel in candidate.channels:
                    channel_scores[channel] = max(
                        channel_scores.get(channel, 0.0), self._bounded(candidate.score)
                    )
            for channel_score in channel_scores.values():
                miss_probability *= 1.0 - channel_score
            relevance = 1.0 - miss_probability
            authority = self._authority(first.trust_level)
            recency = self._recency(first.observed_at, first.recorded_at, watermark)
            total = (
                self.RELEVANCE_WEIGHT * relevance
                + self.AUTHORITY_WEIGHT * authority
                + self.RECENCY_WEIGHT * recency
            )
            components = (
                ("relevance", round(relevance, 6)),
                ("authority", round(authority, 6)),
                ("recency", round(recency, 6)),
                ("relevance_contribution", round(self.RELEVANCE_WEIGHT * relevance, 6)),
                ("authority_contribution", round(self.AUTHORITY_WEIGHT * authority, 6)),
                ("recency_contribution", round(self.RECENCY_WEIGHT * recency, 6)),
            )
            merged.append(RankedHit(
                kind=kind,
                key=key,
                score=round(self._bounded(total), 6),
                channels=tuple(sorted(channel_scores)),
                trust_level=first.trust_level,
                observed_at=first.observed_at,
                recorded_at=first.recorded_at,
                score_components=components,
            ))
        ranked = sorted(merged, key=lambda item: (-item.score, item.kind, item.key))
        return ranked[:limit]
