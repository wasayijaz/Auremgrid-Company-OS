from __future__ import annotations

import math
import re
import hashlib
from collections import Counter
from dataclasses import dataclass
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


class HybridRanker:
    """Fuse keyword, lexical-vector, and graph signals after ACL filtering."""

    def fuse(self, hits: list[RankedHit], limit: int = 8) -> list[RankedHit]:
        merged: dict[tuple[str, str], RankedHit] = {}
        for hit in hits:
            key = (hit.kind, hit.key)
            existing = merged.get(key)
            if existing is None:
                merged[key] = hit
                continue
            merged[key] = RankedHit(
                kind=hit.kind,
                key=hit.key,
                score=round(existing.score + hit.score, 4),
                channels=tuple(dict.fromkeys((*existing.channels, *hit.channels))),
            )
        ranked = sorted(merged.values(), key=lambda item: item.score, reverse=True)
        return ranked[:limit]
