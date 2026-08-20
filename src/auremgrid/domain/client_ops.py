from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from auremgrid.domain.models import iso


@dataclass(frozen=True)
class ClientAccountRosterRole:
    id: str; roster_id: str; organization_id: str; workspace_id: str; role_key: str
    wing: str | None; person_id: str; created_at: datetime
    def to_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "created_at": iso(self.created_at)}


@dataclass(frozen=True)
class ClientAccountRoster:
    id: str; organization_id: str; workspace_id: str; effective_at: datetime
    version: int; created_at: datetime; created_by_person_id: str; note: str
    roles: tuple[ClientAccountRosterRole, ...] = ()
    def to_dict(self) -> dict[str, Any]:
        return {
            **self.__dict__,
            "effective_at": iso(self.effective_at),
            "created_at": iso(self.created_at),
            "roles": [role.to_dict() for role in self.roles],
        }


@dataclass(frozen=True)
class MeetingResponsibilities:
    meeting_id: str; roster_id: str | None; facilitator_person_id: str | None
    note_taker_person_id: str | None; source: dict[str, str | None]
    event_id: str | None = None; event_ids: dict[str, str | None] | None = None
    def to_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "event_ids": self.event_ids or {"facilitator": None, "note_taker": None}}


@dataclass(frozen=True)
class Signal:
    id: str; organization_id: str; workspace_id: str; type: str; source_type: str
    source_id: str | None; evidence: str; confidence: float; classification: str | None
    status: str; routed_to: str | None; created_at: datetime; resolved_at: datetime | None = None
    def to_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "created_at": iso(self.created_at), "resolved_at": iso(self.resolved_at)}


@dataclass(frozen=True)
class Risk:
    id: str; organization_id: str; workspace_id: str; project_id: str | None; type: str
    severity: str; probability: float; impact: str; owner_person_id: str | None; detected_at: datetime
    status: str; evidence: str; recommended_action: str; resolution: str | None = None; resolved_at: datetime | None = None
    def to_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "detected_at": iso(self.detected_at), "resolved_at": iso(self.resolved_at)}


@dataclass(frozen=True)
class Opportunity:
    id: str; organization_id: str; workspace_id: str; type: str; estimated_value: float | None
    reason: str; evidence: str; recommendation: str; owner_person_id: str | None
    status: str; created_at: datetime
    def to_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "created_at": iso(self.created_at)}


@dataclass(frozen=True)
class ClientHealthSnapshot:
    id: str; organization_id: str; workspace_id: str; overall: float; relationship: float
    delivery: float; performance: float | None; finance: float | None; communication: float
    scope: float; sentiment: float | None; contributing_signals: tuple[str, ...]; explanation: str
    previous_score: float | None; trend: str; calculated_at: datetime
    def to_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "contributing_signals": list(self.contributing_signals), "calculated_at": iso(self.calculated_at)}


@dataclass(frozen=True)
class Meeting:
    id: str; organization_id: str; workspace_id: str; title: str; occurred_at: datetime
    summary: str; sentiment: float | None; source: str; recording_url: str | None; created_at: datetime
    def to_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "occurred_at": iso(self.occurred_at), "created_at": iso(self.created_at)}


@dataclass(frozen=True)
class Conversation:
    id: str; organization_id: str; workspace_id: str; source: str; channel: str
    external_thread_id: str | None; subject: str | None; linked_work_item_id: str | None
    linked_decision_id: str | None; linked_risk_id: str | None; created_at: datetime
    def to_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "created_at": iso(self.created_at)}


@dataclass(frozen=True)
class Message:
    id: str; conversation_id: str; sender_type: str; sender_id: str; body: str; sent_at: datetime
    reply_to_id: str | None; sentiment: float | None; requires_reply: bool; replied_at: datetime | None
    important: bool; source_locator: str | None
    def to_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "sent_at": iso(self.sent_at), "replied_at": iso(self.replied_at)}
