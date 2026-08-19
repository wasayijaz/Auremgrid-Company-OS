from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


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
