from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from auremgrid.domain.models import iso


WORK_STATUSES = (
    "captured",
    "assigned",
    "in_progress",
    "review",
    "client_review",
    "shipped",
)

ALLOWED_TRANSITIONS = {
    "captured": {"assigned"},
    "assigned": {"in_progress"},
    "in_progress": {"review"},
    "review": {"in_progress", "client_review"},
    "client_review": {"in_progress", "shipped"},
    "shipped": set(),
}

DEFINITION_OF_DONE = (
    "mobile_responsive",
    "assets_exported",
    "creative_safe_zone",
    "copy_spellchecked",
    "handoff_notes",
)


def default_dod() -> dict[str, bool]:
    return {item: False for item in DEFINITION_OF_DONE}


@dataclass(frozen=True)
class WorkItem:
    id: str
    workspace_id: str
    title: str
    request: str
    requested_by: str
    needed_by: str | None
    status: str
    assignee_id: str | None
    playbook_id: str | None
    decision_maker: str | None
    definition_of_done: dict[str, bool]
    created_at: datetime
    updated_at: datetime
    project_id: str | None = None
    campaign_id: str | None = None
    parent_id: str | None = None
    owner_person_id: str | None = None
    assignee_person_id: str | None = None
    reviewer_person_id: str | None = None
    priority: str = "normal"
    tags: tuple[str, ...] = ()
    estimate_hours: float | None = None
    actual_effort_hours: float = 0.0
    start_date: str | None = None
    deadline: str | None = None
    blocking_reason: str | None = None
    brief: str = ""
    brain_context: str = ""
    financial_value: float | None = None

    @property
    def dod_complete(self) -> bool:
        return all(self.definition_of_done.get(item, False) for item in DEFINITION_OF_DONE)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "title": self.title,
            "request": self.request,
            "requested_by": self.requested_by,
            "needed_by": self.needed_by,
            "status": self.status,
            "assignee_id": self.assignee_id,
            "playbook_id": self.playbook_id,
            "decision_maker": self.decision_maker,
            "definition_of_done": dict(self.definition_of_done),
            "dod_complete": self.dod_complete,
            "created_at": iso(self.created_at),
            "updated_at": iso(self.updated_at),
            "project_id": self.project_id,
            "campaign_id": self.campaign_id,
            "parent_id": self.parent_id,
            "owner_person_id": self.owner_person_id,
            "assignee_person_id": self.assignee_person_id,
            "reviewer_person_id": self.reviewer_person_id,
            "priority": self.priority,
            "tags": list(self.tags),
            "estimate_hours": self.estimate_hours,
            "actual_effort_hours": self.actual_effort_hours,
            "start_date": self.start_date,
            "deadline": self.deadline,
            "blocking_reason": self.blocking_reason,
            "brief": self.brief,
            "brain_context": self.brain_context,
            "financial_value": self.financial_value,
        }


@dataclass(frozen=True)
class WorkEvent:
    id: str
    workspace_id: str
    work_item_id: str
    actor_id: str
    action: str
    from_status: str | None
    to_status: str | None
    detail: str
    recorded_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "work_item_id": self.work_item_id,
            "actor_id": self.actor_id,
            "action": self.action,
            "from_status": self.from_status,
            "to_status": self.to_status,
            "detail": self.detail,
            "recorded_at": iso(self.recorded_at),
        }


@dataclass(frozen=True)
class Touchpoint:
    id: str
    workspace_id: str
    actor_id: str
    kind: str
    summary: str
    occurred_at: datetime
    recorded_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "actor_id": self.actor_id,
            "kind": self.kind,
            "summary": self.summary,
            "occurred_at": iso(self.occurred_at),
            "recorded_at": iso(self.recorded_at),
        }


@dataclass(frozen=True)
class Playbook:
    id: str
    workspace_id: str | None
    slug: str
    title: str
    body: str
    created_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "slug": self.slug,
            "title": self.title,
            "body": self.body,
            "created_at": iso(self.created_at),
        }


@dataclass(frozen=True)
class ClientBrainPack:
    workspace_id: str
    snapshot: str
    brand_rules: str
    landing_pages: str
    ads: str
    design: str
    email: str
    dos: tuple[str, ...]
    donts: tuple[str, ...]
    open_loops: tuple[str, ...]
    updated_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "snapshot": self.snapshot,
            "brand_rules": self.brand_rules,
            "landing_pages": self.landing_pages,
            "ads": self.ads,
            "design": self.design,
            "email": self.email,
            "dos": list(self.dos),
            "donts": list(self.donts),
            "open_loops": list(self.open_loops),
            "updated_at": iso(self.updated_at),
        }


@dataclass(frozen=True)
class StatusPost:
    id: str
    workspace_id: str
    actor_id: str
    body: str
    posted_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "actor_id": self.actor_id,
            "body": self.body,
            "posted_at": iso(self.posted_at),
        }


@dataclass(frozen=True)
class AccountBrief:
    workspace_id: str
    brain: ClientBrainPack | None
    playbooks: tuple[Playbook, ...] = field(default_factory=tuple)
    open_work: tuple[WorkItem, ...] = field(default_factory=tuple)
    latest_touchpoint: Touchpoint | None = None
    days_since_touchpoint: int | None = None
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "brain": self.brain.to_dict() if self.brain else None,
            "playbooks": [playbook.to_dict() for playbook in self.playbooks],
            "open_work": [item.to_dict() for item in self.open_work],
            "latest_touchpoint": self.latest_touchpoint.to_dict() if self.latest_touchpoint else None,
            "days_since_touchpoint": self.days_since_touchpoint,
            "evidence": self.evidence,
        }
