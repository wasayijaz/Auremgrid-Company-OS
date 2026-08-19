from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from auremgrid.domain.models import iso


@dataclass(frozen=True)
class Organization:
    id: str
    name: str
    created_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "created_at": iso(self.created_at)}


@dataclass(frozen=True)
class Person:
    id: str
    organization_id: str
    name: str
    email: str | None
    title: str | None
    department: str | None
    manager_id: str | None
    status: str
    created_at: datetime
    updated_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "created_at": iso(self.created_at), "updated_at": iso(self.updated_at)}


@dataclass(frozen=True)
class OrganizationMembership:
    id: str
    organization_id: str
    person_id: str
    role: str
    created_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "created_at": iso(self.created_at)}


@dataclass(frozen=True)
class WorkspaceMembership:
    id: str
    workspace_id: str
    person_id: str
    role: str
    created_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "created_at": iso(self.created_at)}


@dataclass(frozen=True)
class Project:
    id: str
    organization_id: str
    workspace_id: str
    name: str
    description: str
    owner_person_id: str | None
    status: str
    priority: str
    start_date: str | None
    due_date: str | None
    budget: float | None
    tags: tuple[str, ...]
    health: str
    progress: float
    created_at: datetime
    updated_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.__dict__,
            "tags": list(self.tags),
            "created_at": iso(self.created_at),
            "updated_at": iso(self.updated_at),
        }


@dataclass(frozen=True)
class Deliverable:
    id: str
    organization_id: str
    workspace_id: str
    project_id: str
    work_item_id: str | None
    title: str
    type: str
    owner_person_id: str | None
    current_version: int
    approval_status: str
    preview_url: str | None
    final_url: str | None
    reviewer_person_id: str | None
    client_approver_contact_id: str | None
    revision_count: int
    created_at: datetime
    shipped_at: datetime | None

    def to_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "created_at": iso(self.created_at), "shipped_at": iso(self.shipped_at)}


@dataclass(frozen=True)
class Review:
    id: str
    organization_id: str
    workspace_id: str
    deliverable_id: str
    version: int
    kind: str
    status: str
    reviewer_person_id: str | None
    opened_at: datetime
    closed_at: datetime | None
    decision: str | None

    def to_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "opened_at": iso(self.opened_at), "closed_at": iso(self.closed_at)}


@dataclass(frozen=True)
class ReviewComment:
    id: str
    review_id: str
    author_person_id: str
    body: str
    timestamp_seconds: float | None
    created_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "created_at": iso(self.created_at)}


@dataclass(frozen=True)
class Decision:
    id: str
    organization_id: str
    workspace_id: str | None
    project_id: str | None
    campaign_id: str | None
    statement: str
    rationale: str
    decided_by_person_id: str
    participant_person_ids: tuple[str, ...] = field(default_factory=tuple)
    source_id: str | None = None
    source_locator: str | None = None
    evidence: str = ""
    created_at: datetime | None = None
    effective_from: datetime | None = None
    effective_until: datetime | None = None
    superseded_by: str | None = None
    tags: tuple[str, ...] = field(default_factory=tuple)
    affected_entities: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.__dict__,
            "participant_person_ids": list(self.participant_person_ids),
            "tags": list(self.tags),
            "affected_entities": list(self.affected_entities),
            "created_at": iso(self.created_at),
            "effective_from": iso(self.effective_from),
            "effective_until": iso(self.effective_until),
        }
