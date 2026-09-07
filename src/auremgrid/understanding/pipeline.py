from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any


EXTRACTOR_VERSION = "understanding-v1"


@dataclass(frozen=True)
class FactProposal:
    proposal_id: str
    kind: str
    subject: str
    payload: dict[str, Any]
    evidence: dict[str, Any]
    confidence: float
    extractor_version: str
    status: str = "proposed"


@dataclass(frozen=True)
class DecisionProposal:
    proposal_id: str
    kind: str
    subject: str
    payload: dict[str, Any]
    evidence: dict[str, Any]
    confidence: float
    extractor_version: str
    status: str = "proposed"


@dataclass(frozen=True)
class CommitmentProposal:
    proposal_id: str
    kind: str
    subject: str
    payload: dict[str, Any]
    evidence: dict[str, Any]
    confidence: float
    extractor_version: str
    status: str = "proposed"


@dataclass(frozen=True)
class PreferenceProposal:
    proposal_id: str
    kind: str
    subject: str
    payload: dict[str, Any]
    evidence: dict[str, Any]
    confidence: float
    extractor_version: str
    status: str = "proposed"


@dataclass(frozen=True)
class RequestProposal:
    proposal_id: str
    kind: str
    subject: str
    payload: dict[str, Any]
    evidence: dict[str, Any]
    confidence: float
    extractor_version: str
    status: str = "proposed"


@dataclass(frozen=True)
class MetricProposal:
    proposal_id: str
    kind: str
    subject: str
    payload: dict[str, Any]
    evidence: dict[str, Any]
    confidence: float
    extractor_version: str
    status: str = "proposed"


@dataclass(frozen=True)
class RiskSignal:
    proposal_id: str
    kind: str
    subject: str
    payload: dict[str, Any]
    evidence: dict[str, Any]
    confidence: float
    extractor_version: str
    status: str = "proposed"


@dataclass(frozen=True)
class OpportunitySignal:
    proposal_id: str
    kind: str
    subject: str
    payload: dict[str, Any]
    evidence: dict[str, Any]
    confidence: float
    extractor_version: str
    status: str = "proposed"


@dataclass(frozen=True)
class EntityProposal:
    proposal_id: str
    kind: str
    subject: str
    payload: dict[str, Any]
    evidence: dict[str, Any]
    confidence: float
    extractor_version: str
    status: str = "proposed"


@dataclass(frozen=True)
class RelationshipProposal:
    proposal_id: str
    kind: str
    subject: str
    payload: dict[str, Any]
    evidence: dict[str, Any]
    confidence: float
    extractor_version: str
    status: str = "proposed"


Proposal = (
    FactProposal
    | DecisionProposal
    | CommitmentProposal
    | PreferenceProposal
    | RequestProposal
    | MetricProposal
    | RiskSignal
    | OpportunitySignal
    | EntityProposal
    | RelationshipProposal
)

_STRUCTURED_RE = re.compile(r"^(?P<kind>[A-Z_]+):\s*(?P<body>.+?)\s*$")
_SENTENCE_RE = re.compile(r"[^\n.!?]+[.!?]?")
_METRIC_RE = re.compile(
    r"(?P<label>[A-Za-z][A-Za-z0-9 _/-]{1,60}?)\s*(?:is|was|=|:)\s*(?P<value>\$?\d+(?:\.\d+)?%?)",
    re.IGNORECASE,
)
_RELATION_RE = re.compile(
    r"\b(?P<left>[A-Z][A-Za-z0-9 &.-]{1,60}?)\s+"
    r"(?P<relation>reports to|depends on|owns|owned by|manages|partners with)\s+"
    r"(?P<right>[A-Z][A-Za-z0-9 &.-]{1,60})\b"
)


def _clean(value: str) -> str:
    return " ".join(value.strip(" \t\r\n.:;,-").split())


def _confidence(value: float) -> float:
    return max(0.0, min(1.0, value))


def _evidence(source_ref: str | None, text: str, start: int, end: int) -> dict[str, Any]:
    return {"source_ref": source_ref, "text": text[start:end], "start": start, "end": end}


def _proposal_id(kind: str, subject: str, evidence: dict[str, Any], payload: dict[str, Any]) -> str:
    material = repr((kind, subject, evidence["source_ref"], evidence["start"], evidence["end"], payload))
    return f"up_{hashlib.sha256(material.encode('utf-8')).hexdigest()[:16]}"


def _make(
    cls: type[Proposal],
    kind: str,
    subject: str,
    payload: dict[str, Any],
    evidence: dict[str, Any],
    confidence: float,
) -> Proposal:
    return cls(
        proposal_id=_proposal_id(kind, subject, evidence, payload),
        kind=kind,
        subject=subject,
        payload=payload,
        evidence=evidence,
        confidence=_confidence(confidence),
        extractor_version=EXTRACTOR_VERSION,
    )


def _structured(span: str, evidence: dict[str, Any]) -> list[Proposal]:
    match = _STRUCTURED_RE.match(span.strip())
    if not match:
        return []
    kind = match.group("kind").lower()
    parts = [_clean(part) for part in match.group("body").split("|")]
    proposals: list[Proposal] = []
    if kind == "fact" and len(parts) >= 3:
        payload = {"predicate": parts[1], "object": parts[2]}
        proposals.append(_make(FactProposal, "fact", parts[0], payload, evidence, 0.92))
    elif kind == "decision" and len(parts) >= 2:
        payload = {"decision": parts[1]}
        proposals.append(_make(DecisionProposal, "decision", parts[0], payload, evidence, 0.9))
    elif kind == "commitment" and len(parts) >= 2:
        payload = {"commitment": parts[-1], "owner": parts[1] if len(parts) > 2 else parts[0]}
        proposals.append(_make(CommitmentProposal, "commitment", parts[0], payload, evidence, 0.88))
    elif kind == "preference" and len(parts) >= 2:
        payload = {"preference": parts[1]}
        proposals.append(_make(PreferenceProposal, "preference", parts[0], payload, evidence, 0.88))
    elif kind == "request" and len(parts) >= 2:
        payload = {"request": parts[1]}
        proposals.append(_make(RequestProposal, "request", parts[0], payload, evidence, 0.87))
    elif kind == "metric" and len(parts) >= 3:
        payload = {"metric": parts[1], "value": parts[2]}
        proposals.append(_make(MetricProposal, "metric", parts[0], payload, evidence, 0.91))
    elif kind == "risk" and len(parts) >= 2:
        payload = {"risk": parts[1]}
        proposals.append(_make(RiskSignal, "risk", parts[0], payload, evidence, 0.86))
    elif kind == "opportunity" and len(parts) >= 2:
        payload = {"opportunity": parts[1]}
        proposals.append(_make(OpportunitySignal, "opportunity", parts[0], payload, evidence, 0.86))
    elif kind == "entity" and len(parts) >= 1:
        payload = {"entity_type": parts[1] if len(parts) > 1 else "unknown"}
        proposals.append(_make(EntityProposal, "entity", parts[0], payload, evidence, 0.89))
    elif kind in {"rel", "relationship"} and len(parts) >= 3:
        payload = {"from_entity": parts[0], "relation": parts[1], "to_entity": parts[2]}
        proposals.append(_make(RelationshipProposal, "relationship", parts[0], payload, evidence, 0.9))
    return proposals


def _natural(span: str, evidence: dict[str, Any]) -> list[Proposal]:
    cleaned = _clean(span)
    lowered = cleaned.lower()
    subject = cleaned.split(" ", 1)[0] if cleaned else "source"
    proposals: list[Proposal] = []

    if not cleaned:
        return proposals
    if "decided" in lowered or lowered.startswith("decision "):
        proposals.append(_make(DecisionProposal, "decision", subject, {"decision": cleaned}, evidence, 0.72))
    if "commit" in lowered or re.search(r"\b(i|we|they|team)\s+will\b", lowered):
        proposals.append(_make(CommitmentProposal, "commitment", subject, {"commitment": cleaned}, evidence, 0.7))
    if any(marker in lowered for marker in (" prefer ", " prefers ", " likes ", " wants ", " avoid ", " hates ")):
        proposals.append(_make(PreferenceProposal, "preference", subject, {"preference": cleaned}, evidence, 0.68))
    if lowered.startswith(("please ", "can you ", "could you ")) or " request " in f" {lowered} ":
        proposals.append(_make(RequestProposal, "request", subject, {"request": cleaned}, evidence, 0.68))
    if any(marker in lowered for marker in ("risk", "blocker", "concern", "at risk")):
        proposals.append(_make(RiskSignal, "risk", subject, {"risk": cleaned}, evidence, 0.66))
    if "opportunity" in lowered or "upsell" in lowered:
        proposals.append(_make(OpportunitySignal, "opportunity", subject, {"opportunity": cleaned}, evidence, 0.66))
    metric = _METRIC_RE.search(cleaned)
    if metric:
        proposals.append(
            _make(
                MetricProposal,
                "metric",
                _clean(metric.group("label")),
                {"metric": _clean(metric.group("label")), "value": metric.group("value")},
                evidence,
                0.7,
            )
        )
    relation = _RELATION_RE.search(cleaned)
    if relation:
        payload = {
            "from_entity": _clean(relation.group("left")),
            "relation": _clean(relation.group("relation")).lower(),
            "to_entity": _clean(relation.group("right")),
        }
        proposals.append(_make(RelationshipProposal, "relationship", payload["from_entity"], payload, evidence, 0.72))
    return proposals


def extract(text: str, source_ref: str | None = None) -> list[Proposal]:
    proposals: list[Proposal] = []
    seen: set[str] = set()
    for match in _SENTENCE_RE.finditer(text):
        span = match.group(0)
        if not span.strip():
            continue
        evidence = _evidence(source_ref, text, match.start(), match.end())
        for proposal in [*_structured(span, evidence), *_natural(span, evidence)]:
            if proposal.proposal_id not in seen:
                proposals.append(proposal)
                seen.add(proposal.proposal_id)
    return proposals
