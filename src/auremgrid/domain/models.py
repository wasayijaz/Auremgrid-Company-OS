from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class AgentLevel(StrEnum):
    L0_EXECUTE = "L0"
    L1_OPERATE = "L1"
    L2_BUILD = "L2"
    L3_REASON = "L3"


AGENT_LEVEL_ORDER: tuple[AgentLevel, ...] = (
    AgentLevel.L0_EXECUTE,
    AgentLevel.L1_OPERATE,
    AgentLevel.L2_BUILD,
    AgentLevel.L3_REASON,
)


@dataclass(frozen=True)
class LevelDefinition:
    level: AgentLevel
    name: str
    intent: str
    cost_weight: float
    capability_tags: tuple[str, ...]
    can_handle: tuple[AgentLevel, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level.value,
            "name": self.name,
            "intent": self.intent,
            "cost_weight": self.cost_weight,
            "capability_tags": list(self.capability_tags),
            "can_handle": [level.value for level in self.can_handle],
        }


LEVEL_DEFINITIONS: dict[AgentLevel, LevelDefinition] = {
    AgentLevel.L0_EXECUTE: LevelDefinition(
        level=AgentLevel.L0_EXECUTE,
        name="Execute",
        intent="Fast, high-throughput execution of well-defined, repeatable tasks",
        cost_weight=0.25,
        capability_tags=("execute", "format", "extract", "summarize", "draft"),
        can_handle=(AgentLevel.L0_EXECUTE,),
    ),
    AgentLevel.L1_OPERATE: LevelDefinition(
        level=AgentLevel.L1_OPERATE,
        name="Operate",
        intent="Standard operational judgment, routing, communication, and coordination",
        cost_weight=0.5,
        capability_tags=("reason", "produce", "communicate", "route", "schedule"),
        can_handle=(AgentLevel.L0_EXECUTE, AgentLevel.L1_OPERATE),
    ),
    AgentLevel.L2_BUILD: LevelDefinition(
        level=AgentLevel.L2_BUILD,
        name="Build",
        intent="Deep analysis, implementation, and verification requiring sustained reasoning",
        cost_weight=1.0,
        capability_tags=("build", "verify", "review", "diagnose", "implement"),
        can_handle=(AgentLevel.L0_EXECUTE, AgentLevel.L1_OPERATE, AgentLevel.L2_BUILD),
    ),
    AgentLevel.L3_REASON: LevelDefinition(
        level=AgentLevel.L3_REASON,
        name="Reason",
        intent="Strategic decisions, complex synthesis, architecture, and risk detection",
        cost_weight=2.0,
        capability_tags=("strategize", "architect", "assess_risk", "synthesize", "decide"),
        can_handle=(
            AgentLevel.L0_EXECUTE,
            AgentLevel.L1_OPERATE,
            AgentLevel.L2_BUILD,
            AgentLevel.L3_REASON,
        ),
    ),
}

CAPABILITY_LEVELS: dict[str, AgentLevel] = {
    tag: definition.level
    for definition in LEVEL_DEFINITIONS.values()
    for tag in definition.capability_tags
}


def normalize_agent_level(value: AgentLevel | str) -> AgentLevel:
    try:
        return value if isinstance(value, AgentLevel) else AgentLevel(str(value))
    except ValueError as exc:
        raise ValueError(f"unknown agent level: {value}") from exc


def effective_capability_tags(level: AgentLevel | str) -> tuple[str, ...]:
    normalized = normalize_agent_level(level)
    tags: list[str] = []
    for handled_level in LEVEL_DEFINITIONS[normalized].can_handle:
        tags.extend(LEVEL_DEFINITIONS[handled_level].capability_tags)
    return tuple(dict.fromkeys(tags))


def iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


@dataclass(frozen=True)
class Workspace:
    id: str
    name: str
    created_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "created_at": iso(self.created_at)}


@dataclass(frozen=True)
class Actor:
    id: str
    workspace_id: str
    name: str
    role: str
    created_at: datetime

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def can_write(self) -> bool:
        return self.role in {"admin", "operator"}

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "name": self.name,
            "role": self.role,
            "created_at": iso(self.created_at),
        }


@dataclass(frozen=True)
class SourceArtifact:
    id: str
    workspace_id: str
    source_key: str
    locator: str
    content_hash: str
    media_type: str
    trust_level: str
    allowed_actor_ids: tuple[str, ...]
    observed_at: datetime
    recorded_at: datetime
    version: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "source_key": self.source_key,
            "locator": self.locator,
            "content_hash": self.content_hash,
            "media_type": self.media_type,
            "trust_level": self.trust_level,
            "allowed_actor_ids": list(self.allowed_actor_ids),
            "observed_at": iso(self.observed_at),
            "recorded_at": iso(self.recorded_at),
            "version": self.version,
        }


@dataclass(frozen=True)
class Document:
    id: str
    workspace_id: str
    source_id: str
    content: str
    content_hash: str
    observed_at: datetime
    recorded_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "source_id": self.source_id,
            "content": self.content,
            "content_hash": self.content_hash,
            "observed_at": iso(self.observed_at),
            "recorded_at": iso(self.recorded_at),
        }


@dataclass(frozen=True)
class Citation:
    source_id: str
    source_key: str
    locator: str
    content_hash: str
    evidence_span: str
    observed_at: datetime
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    confidence: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "source_key": self.source_key,
            "locator": self.locator,
            "content_hash": self.content_hash,
            "evidence_span": self.evidence_span,
            "observed_at": iso(self.observed_at),
            "valid_from": iso(self.valid_from),
            "valid_until": iso(self.valid_until),
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class Fact:
    id: str
    workspace_id: str
    source_id: str
    document_id: str
    subject: str
    predicate: str
    object: str
    valid_from: datetime
    valid_until: datetime | None
    observed_at: datetime
    recorded_at: datetime
    confidence: float
    superseded_by: str | None
    conflict_group: str | None
    citation: Citation

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "source_id": self.source_id,
            "document_id": self.document_id,
            "subject": self.subject,
            "predicate": self.predicate,
            "object": self.object,
            "valid_from": iso(self.valid_from),
            "valid_until": iso(self.valid_until),
            "observed_at": iso(self.observed_at),
            "recorded_at": iso(self.recorded_at),
            "confidence": self.confidence,
            "superseded_by": self.superseded_by,
            "conflict_group": self.conflict_group,
            "citation": self.citation.to_dict(),
        }


@dataclass(frozen=True)
class Relation:
    id: str
    workspace_id: str
    source_id: str
    document_id: str
    from_entity: str
    relation: str
    to_entity: str
    valid_from: datetime
    valid_until: datetime | None
    observed_at: datetime
    recorded_at: datetime
    confidence: float
    citation: Citation

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "source_id": self.source_id,
            "document_id": self.document_id,
            "from_entity": self.from_entity,
            "relation": self.relation,
            "to_entity": self.to_entity,
            "valid_from": iso(self.valid_from),
            "valid_until": iso(self.valid_until),
            "observed_at": iso(self.observed_at),
            "recorded_at": iso(self.recorded_at),
            "confidence": self.confidence,
            "citation": self.citation.to_dict(),
        }


@dataclass(frozen=True)
class Memory:
    id: str
    workspace_id: str
    actor_id: str
    kind: str
    content: str
    observed_at: datetime
    recorded_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "actor_id": self.actor_id,
            "kind": self.kind,
            "content": self.content,
            "observed_at": iso(self.observed_at),
            "recorded_at": iso(self.recorded_at),
        }


@dataclass(frozen=True)
class AuditEvent:
    id: str
    workspace_id: str
    actor_id: str
    action: str
    target: str
    outcome: str
    detail: str
    recorded_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "actor_id": self.actor_id,
            "action": self.action,
            "target": self.target,
            "outcome": self.outcome,
            "detail": self.detail,
            "recorded_at": iso(self.recorded_at),
        }


@dataclass(frozen=True)
class EvidenceItem:
    kind: str
    score: float
    payload: dict[str, Any]
    citation: Citation

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "score": self.score,
            "payload": self.payload,
            "citation": self.citation.to_dict(),
        }


@dataclass(frozen=True)
class EvidenceBundle:
    workspace_id: str
    query: str
    as_of: datetime
    unknown: bool
    message: str
    items: tuple[EvidenceItem, ...] = field(default_factory=tuple)
    retrieval: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "query": self.query,
            "as_of": iso(self.as_of),
            "unknown": self.unknown,
            "message": self.message,
            "retrieval": self.retrieval,
            "items": [item.to_dict() for item in self.items],
        }


@dataclass(frozen=True)
class IngestResult:
    created: bool
    source: SourceArtifact
    document_id: str | None
    fact_ids: tuple[str, ...] = ()
    relation_ids: tuple[str, ...] = ()
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "created": self.created,
            "source": self.source.to_dict(),
            "document_id": self.document_id,
            "fact_ids": list(self.fact_ids),
            "relation_ids": list(self.relation_ids),
            "message": self.message,
        }
