from __future__ import annotations

import json
from typing import Any, Callable

from auremgrid.domain.errors import AuthorizationError, NotFoundError, ValidationError
from auremgrid.domain.intelligence_contracts import IntelligenceRunbook
from auremgrid.services.intelligence_contracts import IntelligenceContractService, _hash, _json, _loads, _now


_RUNBOOK_COLUMNS = (
    "id,version,name,trigger,required_domains_json,required_evidence_json,specialists_json,topology,"
    "stages_json,quality_gates_json,contradiction_policy,scenario_policy,escalation_policy,max_iterations,"
    "output_contract_json,capability_level,summary,intent,domains_json,profile_ids_json,"
    "activation_sequence_json,steps_json,handoff_gates_json,required_inputs_json,outputs_json,"
    "stop_conditions_json,allowed_tool_refs_json,status,content_hash,created_at"
)

_CUSTOMIZABLE_FIELDS = {"name", "summary", "intent", "domains", "profile_ids", "activation_sequence"}


def _text_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item.strip() for item in value):
        raise ValidationError(f"{field} must be a non-empty list of strings")
    return [item.strip() for item in value]


class IntelligenceGovernanceService:
    """Human-review gate for evaluated-outcome lessons and runbook approvals."""

    def __init__(self, os: Any, new_id: Callable[[str], str]) -> None:
        self.os = os
        self.conn = os.store.conn
        self.new_id = new_id
        self._contracts = IntelligenceContractService(os)

    # ------------------------------------------------------------------ runbooks

    def list_runbooks_with_states(self, organization_id: str, workspace_id: str, person_id: str) -> dict[str, Any]:
        self.os._require_person_access(organization_id, workspace_id, person_id)
        rows = self.conn.execute(
            "SELECT * FROM intelligence_runbooks WHERE status='active' ORDER BY id, version"
        ).fetchall()
        runbooks = []
        for row in rows:
            runbook = self._contracts._runbook_from_row(row)
            runbooks.append({
                **runbook.to_dict(),
                "approval_state": self.runbook_approval_state(organization_id, workspace_id, runbook.id, runbook.version),
            })
        return {
            "scope": {"organization_id": organization_id, "workspace_id": workspace_id},
            "runbooks": runbooks,
        }

    def runbook_approval_state(
        self, organization_id: str, workspace_id: str | None, runbook_id: str, version: int
    ) -> dict[str, Any]:
        row = self.conn.execute(
            """SELECT * FROM intelligence_runbook_approvals
               WHERE organization_id=? AND workspace_id IS ? AND runbook_id=? AND runbook_version=?
               ORDER BY created_at DESC, rowid DESC LIMIT 1""",
            (organization_id, workspace_id, runbook_id, str(version)),
        ).fetchone()
        if row is None:
            return {
                "runbook_id": runbook_id,
                "runbook_version": str(version),
                "status": "draft",
                "approved_by": None,
                "approved_at": None,
                "updated_at": None,
            }
        return {
            "runbook_id": row["runbook_id"],
            "runbook_version": row["runbook_version"],
            "status": row["status"],
            "approved_by": row["approved_by"],
            "approved_at": row["approved_at"],
            "updated_at": row["updated_at"],
        }

    def require_runbook_approved(
        self, organization_id: str, workspace_id: str | None, runbook_id: str, version: int
    ) -> None:
        state = self.runbook_approval_state(organization_id, workspace_id, runbook_id, version)
        if state["status"] != "approved":
            raise AuthorizationError(f"runbook {runbook_id} v{version} is not approved for execution")

    def decide_runbook(
        self,
        organization_id: str,
        workspace_id: str,
        person_id: str,
        runbook_id: str,
        version: int,
        action: str,
    ) -> dict[str, Any]:
        self._require_owner(organization_id, person_id)
        self.os._require_person_access(organization_id, workspace_id, person_id, write=True)
        runbook_id = str(runbook_id).strip()
        version = int(version)
        if action not in {"approve", "retire"}:
            raise ValidationError("action must be approve or retire")
        if not self.conn.execute(
            "SELECT 1 FROM intelligence_runbooks WHERE id=? AND version=? AND status='active'",
            (runbook_id, version),
        ).fetchone():
            raise NotFoundError(f"intelligence runbook not found: {runbook_id}")
        current = self.runbook_approval_state(organization_id, workspace_id, runbook_id, version)
        target = "approved" if action == "approve" else "retired"
        if current["status"] == target:
            raise ValidationError(f"runbook {runbook_id} v{version} is already {target}")
        now = _now()
        item = {
            "id": self.new_id("irba"),
            "organization_id": organization_id,
            "workspace_id": workspace_id,
            "runbook_id": runbook_id,
            "runbook_version": str(version),
            "status": target,
            "approved_by": person_id,
            "approved_at": now,
            "created_at": now,
            "updated_at": now,
        }
        self.conn.execute(
            """INSERT INTO intelligence_runbook_approvals(
                id,organization_id,workspace_id,runbook_id,runbook_version,status,approved_by,approved_at,
                created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            tuple(item.values()),
        )
        self.conn.commit()
        return {
            "runbook_id": runbook_id,
            "runbook_version": str(version),
            "status": target,
            "approved_by": person_id,
            "approved_at": now,
            "updated_at": now,
        }

    def customize_runbook(
        self,
        organization_id: str,
        workspace_id: str,
        person_id: str,
        runbook_id: str,
        patch: dict[str, Any],
    ) -> dict[str, Any]:
        self._require_owner(organization_id, person_id)
        self.os._require_person_access(organization_id, workspace_id, person_id, write=True)
        runbook_id = str(runbook_id).strip()
        if not isinstance(patch, dict) or not patch:
            raise ValidationError("patch must be a non-empty object")
        unknown = set(patch) - _CUSTOMIZABLE_FIELDS
        if unknown:
            raise ValidationError(f"patch fields are not customizable: {sorted(unknown)}")
        base_row = self.conn.execute(
            "SELECT * FROM intelligence_runbooks WHERE id=? AND status='active' ORDER BY version DESC LIMIT 1",
            (runbook_id,),
        ).fetchone()
        if base_row is None:
            raise NotFoundError(f"intelligence runbook not found: {runbook_id}")
        payload = self._contracts._runbook_from_row(base_row).to_dict()
        payload["version"] = int(self.conn.execute(
            "SELECT MAX(version) FROM intelligence_runbooks WHERE id=?", (runbook_id,)
        ).fetchone()[0]) + 1
        for field in ("name", "summary", "intent"):
            if field in patch:
                value = patch[field]
                if not isinstance(value, str) or not value.strip():
                    raise ValidationError(f"patch.{field} must be a non-empty string")
                payload[field] = value.strip()
        if "domains" in patch:
            payload["domains"] = _text_list(patch["domains"], "patch.domains")
            payload["required_domains"] = list(payload["domains"])
        if "profile_ids" in patch:
            profile_ids = _text_list(patch["profile_ids"], "patch.profile_ids")
            for profile_id in profile_ids:
                if not self.conn.execute(
                    "SELECT 1 FROM expert_profiles WHERE id=? AND status='active'", (profile_id,)
                ).fetchone():
                    raise ValidationError(f"unknown expert profile: {profile_id}")
            payload["profile_ids"] = profile_ids
            payload["specialists"] = list(profile_ids)
        if "activation_sequence" in patch:
            payload["activation_sequence"] = _text_list(patch["activation_sequence"], "patch.activation_sequence")
        runbook = IntelligenceRunbook.from_mapping(payload)
        content_hash = _hash(payload)
        now = _now()
        row = {
            "id": runbook.id,
            "version": runbook.version,
            "name": runbook.name,
            "trigger": runbook.trigger,
            "required_domains_json": _json(runbook.required_domains),
            "required_evidence_json": _json(runbook.required_evidence),
            "specialists_json": _json(runbook.specialists),
            "topology": runbook.topology,
            "stages_json": _json([step.to_dict() for step in runbook.stages]),
            "quality_gates_json": _json(runbook.quality_gates),
            "contradiction_policy": runbook.contradiction_policy,
            "scenario_policy": runbook.scenario_policy,
            "escalation_policy": runbook.escalation_policy,
            "max_iterations": runbook.max_iterations,
            "output_contract_json": _json(runbook.output_contract),
            "capability_level": runbook.capability_level,
            "summary": runbook.summary,
            "intent": runbook.intent,
            "domains_json": _json(runbook.domains),
            "profile_ids_json": _json(runbook.profile_ids),
            "activation_sequence_json": _json(runbook.activation_sequence),
            "steps_json": _json([step.to_dict() for step in runbook.steps]),
            "handoff_gates_json": _json(runbook.handoff_gates),
            "required_inputs_json": _json(runbook.required_inputs),
            "outputs_json": _json(runbook.outputs),
            "stop_conditions_json": _json(runbook.stop_conditions),
            "allowed_tool_refs_json": _json(runbook.allowed_tool_refs),
            "status": runbook.status,
            "content_hash": content_hash,
            "created_at": now,
        }
        self.conn.execute(
            f"INSERT INTO intelligence_runbooks({_RUNBOOK_COLUMNS}) "
            f"VALUES ({','.join('?' for _ in row)})",
            tuple(row.values()),
        )
        approval = {
            "id": self.new_id("irba"),
            "organization_id": organization_id,
            "workspace_id": workspace_id,
            "runbook_id": runbook.id,
            "runbook_version": str(runbook.version),
            "status": "draft",
            "approved_by": person_id,
            "approved_at": None,
            "created_at": now,
            "updated_at": now,
        }
        self.conn.execute(
            """INSERT INTO intelligence_runbook_approvals(
                id,organization_id,workspace_id,runbook_id,runbook_version,status,approved_by,approved_at,
                created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            tuple(approval.values()),
        )
        self.conn.commit()
        return {
            **runbook.to_dict(),
            "approval_state": self.runbook_approval_state(organization_id, workspace_id, runbook.id, runbook.version),
        }

    # ------------------------------------------------------------------- lessons

    def proposed_lessons(self, organization_id: str, workspace_id: str, person_id: str) -> dict[str, Any]:
        self._require_owner(organization_id, person_id)
        self.os._require_person_access(organization_id, workspace_id, person_id)
        rows = self.conn.execute(
            """SELECT * FROM intelligence_hypotheses
               WHERE organization_id=? AND workspace_id=? AND status='proposed'
                 AND NOT EXISTS (
                     SELECT 1 FROM intelligence_hypotheses superseding
                     WHERE superseding.supersedes_hypothesis_id = intelligence_hypotheses.id
                 )
               ORDER BY created_at, id""",
            (organization_id, workspace_id),
        ).fetchall()
        lessons = []
        for row in rows:
            outcome = json.loads(row["outcome_json"] or "null")
            if not isinstance(outcome, dict) or outcome.get("origin") != "evaluated_outcome_lesson":
                continue
            lessons.append({
                "id": row["id"],
                "text": row["text"],
                "subject": row["subject"],
                "confidence": row["confidence"],
                "evidence_for_refs": _loads(row["evidence_for_refs_json"]),
                "outcome": outcome,
                "recorded_by_person_id": row["recorded_by_person_id"],
                "created_at": row["created_at"],
            })
        return {
            "scope": {"organization_id": organization_id, "workspace_id": workspace_id},
            "lessons": lessons,
        }

    def decide_lesson(
        self, organization_id: str, workspace_id: str, person_id: str, lesson_id: str, decision: str
    ) -> dict[str, Any]:
        self._require_owner(organization_id, person_id)
        self.os._require_person_access(organization_id, workspace_id, person_id, write=True)
        lesson_id = str(lesson_id).strip()
        if decision not in {"approve", "reject"}:
            raise ValidationError("decision must be approve or reject")
        row = self.conn.execute(
            "SELECT * FROM intelligence_hypotheses WHERE organization_id=? AND workspace_id=? AND id=?",
            (organization_id, workspace_id, lesson_id),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"lesson not found: {lesson_id}")
        outcome = json.loads(row["outcome_json"] or "null")
        if not isinstance(outcome, dict) or outcome.get("origin") != "evaluated_outcome_lesson":
            raise ValidationError("only evaluated-outcome lessons are decided through governance")
        if row["status"] != "proposed" or self.conn.execute(
            "SELECT 1 FROM intelligence_hypotheses WHERE supersedes_hypothesis_id=?", (lesson_id,)
        ).fetchone():
            raise ValidationError(f"lesson {lesson_id} has already been decided")
        superseding = self.os.intelligence_learning.record_hypothesis(
            organization_id, workspace_id, person_id, row["text"],
            subject=row["subject"],
            evidence_for_refs=_loads(row["evidence_for_refs_json"]),
            status="supported" if decision == "approve" else "refuted",
            confidence=float(row["confidence"]),
            supersedes_hypothesis_id=lesson_id,
        )
        return {"lesson_id": lesson_id, "decision": decision, "hypothesis": superseding}

    def _require_owner(self, organization_id: str, person_id: str) -> None:
        membership = self.os.company.org_membership(organization_id, person_id)
        if membership is None or membership.role not in {"owner", "admin"}:
            raise AuthorizationError("organization admin required")
