from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from auremgrid.domain.errors import ValidationError
from auremgrid.domain.security import AuthenticatedIdentity


PILOT_VERDICTS = frozenset({"positive", "negative", "mixed", "unknown", "not_applicable"})


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} is required")
    return value.strip()


class PilotFeedbackService:
    def __init__(self, conn: Any, new_id: Callable[[str], str]) -> None:
        self.conn = conn
        self.new_id = new_id

    def record_verdict(
        self,
        identity: AuthenticatedIdentity,
        *,
        workspace_id: str | None,
        scenario_id: str,
        verdict: str,
        notes: str | None = None,
    ) -> dict[str, Any]:
        if workspace_id is not None:
            identity = self._scope_identity(identity, workspace_id)
        scenario_id = _required_text(scenario_id, "scenario_id")
        verdict = _required_text(verdict, "verdict")
        if verdict not in PILOT_VERDICTS:
            raise ValidationError("verdict must be positive, negative, mixed, unknown, or not_applicable")
        note_text = notes.strip() if isinstance(notes, str) and notes.strip() else None
        item = {
            "id": self.new_id("pilotverdict"),
            "organization_id": identity.organization_id,
            "workspace_id": workspace_id,
            "scenario_id": scenario_id,
            "verdict": verdict,
            "notes": note_text,
            "recorded_by_person_id": identity.person_id,
            "created_at": _now(),
        }
        self.conn.execute(
            """INSERT INTO pilot_operator_verdicts(
                id,organization_id,workspace_id,scenario_id,verdict,notes,recorded_by_person_id,created_at
            ) VALUES (?,?,?,?,?,?,?,?)""",
            tuple(item.values()),
        )
        self.conn.commit()
        return item

    def _scope_identity(self, identity: AuthenticatedIdentity, workspace_id: str) -> AuthenticatedIdentity:
        if identity.workspace_id is not None and identity.workspace_id != workspace_id:
            raise ValidationError("identity workspace mismatch")
        identity.require("workspace_write")
        return identity
