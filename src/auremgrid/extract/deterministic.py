from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime


FACT_RE = re.compile(
    r"^FACT:\s*(?P<subject>.+?)\s*\|\s*(?P<predicate>.+?)\s*\|\s*(?P<object>.+?)\s*$"
)
RELATION_RE = re.compile(
    r"^REL:\s*(?P<from_entity>.+?)\s*\|\s*(?P<relation>.+?)\s*\|\s*(?P<to_entity>.+?)\s*$"
)
META_RE = re.compile(
    r"^META:\s*(?P<key>[A-Za-z0-9_\-]+)\s*=\s*(?P<value>.+?)\s*$"
)


@dataclass(frozen=True)
class ExtractedFact:
    subject: str
    predicate: str
    object: str
    evidence_span: str
    valid_from: datetime
    valid_until: datetime | None
    confidence: float
    conflict_group: str | None


@dataclass(frozen=True)
class ExtractedRelation:
    from_entity: str
    relation: str
    to_entity: str
    evidence_span: str
    valid_from: datetime
    valid_until: datetime | None
    confidence: float


@dataclass(frozen=True)
class Extraction:
    facts: tuple[ExtractedFact, ...]
    relations: tuple[ExtractedRelation, ...]


def _parse_dt(value: str | None, fallback: datetime) -> datetime:
    if not value:
        return fallback
    return datetime.fromisoformat(value)


def extract_claims(content: str, observed_at: datetime) -> Extraction:
    facts: list[ExtractedFact] = []
    relations: list[ExtractedRelation] = []
    current_from = observed_at
    current_until: datetime | None = None
    current_confidence = 0.9
    current_conflict: str | None = None

    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        meta = META_RE.match(line)
        if meta:
            key = meta.group("key").lower()
            value = meta.group("value").strip()
            if key == "valid_from":
                current_from = _parse_dt(value, observed_at)
            elif key == "valid_until":
                current_until = _parse_dt(value, observed_at) if value.lower() not in {"none", "null", ""} else None
            elif key == "confidence":
                current_confidence = float(value)
            elif key == "conflict_group":
                current_conflict = value or None
            continue
        fact = FACT_RE.match(line)
        if fact:
            facts.append(
                ExtractedFact(
                    subject=fact.group("subject").strip(),
                    predicate=fact.group("predicate").strip(),
                    object=fact.group("object").strip(),
                    evidence_span=line,
                    valid_from=current_from,
                    valid_until=current_until,
                    confidence=current_confidence,
                    conflict_group=current_conflict,
                )
            )
            continue
        relation = RELATION_RE.match(line)
        if relation:
            relations.append(
                ExtractedRelation(
                    from_entity=relation.group("from_entity").strip(),
                    relation=relation.group("relation").strip(),
                    to_entity=relation.group("to_entity").strip(),
                    evidence_span=line,
                    valid_from=current_from,
                    valid_until=current_until,
                    confidence=current_confidence,
                )
            )
    return Extraction(facts=tuple(facts), relations=tuple(relations))
