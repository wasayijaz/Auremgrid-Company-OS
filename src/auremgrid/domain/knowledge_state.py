"""The confidence lifecycle for durable knowledge."""

from enum import Enum
from typing import Any

from auremgrid.domain.errors import ValidationError


class KnowledgeState(str, Enum):
    VERIFIED = "verified"
    HIGH_CONFIDENCE = "high_confidence"
    INFERRED = "inferred"
    CONFLICTED = "conflicted"
    STALE = "stale"
    PROPOSED = "proposed"


KNOWLEDGE_STATES = frozenset(item.value for item in KnowledgeState)

# Repeating a state is deliberately legal: an event can refresh its evidence.
# Stale and conflicted are recoverable conditions, while proposed is an
# intake state and cannot be reintroduced after a decision.
KNOWLEDGE_STATE_TRANSITIONS = {
    "proposed": frozenset({"proposed", "inferred", "high_confidence", "verified", "conflicted", "stale"}),
    "inferred": frozenset({"inferred", "high_confidence", "verified", "conflicted", "stale"}),
    "high_confidence": frozenset({"high_confidence", "verified", "conflicted", "stale"}),
    "verified": frozenset({"verified", "conflicted", "stale"}),
    "conflicted": frozenset({"conflicted", "verified", "stale"}),
    "stale": frozenset({"stale", "inferred", "high_confidence", "verified", "conflicted"}),
}


def normalize_knowledge_state(value: Any) -> str:
    """Return a storage value or raise the domain validation error."""
    candidate = value.value if isinstance(value, KnowledgeState) else value
    if not isinstance(candidate, str) or candidate not in KNOWLEDGE_STATES:
        raise ValidationError(f"invalid knowledge state: {value!r}")
    return candidate


def validate_knowledge_transition(previous: str | None, current: Any) -> str:
    """Validate and normalize a state transition."""
    current_value = normalize_knowledge_state(current)
    if previous is None:
        return current_value
    previous_value = normalize_knowledge_state(previous)
    if current_value not in KNOWLEDGE_STATE_TRANSITIONS[previous_value]:
        raise ValidationError(f"invalid knowledge state transition: {previous_value} -> {current_value}")
    return current_value


__all__ = [
    "KnowledgeState",
    "KNOWLEDGE_STATES",
    "KNOWLEDGE_STATE_TRANSITIONS",
    "normalize_knowledge_state",
    "validate_knowledge_transition",
]
