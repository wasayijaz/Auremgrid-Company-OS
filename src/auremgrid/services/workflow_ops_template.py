from __future__ import annotations
from auremgrid.services.workflow_ops_shared import *

class WorkflowOpsTemplateMixin:
    def _normalize_template(self, template: Any) -> dict[str, Any]:
            raw = _obj(template)
            key = _required_text(raw.get("key") or raw.get("id") or raw.get("slug"), "workflow key")
            name = _required_text(raw.get("name") or raw.get("title"), "workflow name")
            version = _required_text(raw.get("version") or "1", "workflow version")
            raw_stages = raw.get("stages") or raw.get("steps")
            if not isinstance(raw_stages, (list, tuple)) or not raw_stages:
                raise ValidationError("workflow template requires at least one stage")
    
            stages: list[dict[str, Any]] = []
            seen: set[str] = set()
            for index, raw_stage in enumerate(raw_stages):
                stage = _obj(raw_stage)
                assignee = _obj(stage.get("assignee"))
                handoff_value = stage.get("handoff_to")
                handoff = _obj(handoff_value) if isinstance(handoff_value, dict) or hasattr(handoff_value, "to_dict") or is_dataclass(handoff_value) else {}
                handoff_target = stage.get("handoff_target")
                # Legacy handoff_target is opaque free text. Keep it for existing
                # behavior, but do not use it to infer a roster assignee.
                structured_handoff_wing = handoff.get("wing") or stage.get("handoff_to_wing")
                structured_handoff_role = handoff.get("role") or stage.get("handoff_to_role")
                stage_key = _required_text(stage.get("key") or stage.get("id") or stage.get("slug"), "stage key")
                if stage_key in seen:
                    raise ValidationError("workflow stage keys must be unique")
                seen.add(stage_key)
                required_evidence = stage.get("required_evidence") or stage.get("evidence_required") or []
                if not isinstance(required_evidence, (list, tuple)):
                    raise ValidationError("required evidence must be a list")
                normalized_evidence = [_required_text(item, "required evidence kind") for item in required_evidence]
                dependencies = stage.get("depends_on") or stage.get("dependencies") or stage.get("after") or []
                if isinstance(dependencies, str):
                    dependencies = [dependencies]
                if not isinstance(dependencies, (list, tuple)):
                    raise ValidationError("stage dependencies must be a list")
                handoff_contract = str(
                    stage.get("handoff_contract")
                    or stage.get("artifact_contract")
                    or handoff.get("artifact_contract")
                    or stage.get("completion_outcome")
                    or ", ".join(normalized_evidence)
                    or ""
                )
                handoff_to_wing = handoff.get("wing") or stage.get("handoff_to_wing") or handoff_target
                handoff_to_role = handoff.get("role") or stage.get("handoff_to_role")
                handoff_to_person_id = handoff.get("person_id") or handoff.get("person") or stage.get("handoff_to_person_id")
                if (handoff_to_wing or handoff_to_role or handoff_to_person_id) and not handoff_contract.strip():
                    raise ValidationError("handoff stages require an artifact contract")
                on_reject_stage_key = stage.get("on_reject_stage_key") or stage.get("on_reject_stage_id")
                approval_gate = stage.get("approval_gate")
                requires_approval = stage.get("requires_approval", stage.get("approval_required", False))
                if approval_gate is not None:
                    requires_approval = approval_gate != "none"
                sla_hours = stage.get("sla_hours")
                if sla_hours is not None and (not isinstance(sla_hours, (int, float)) or isinstance(sla_hours, bool) or sla_hours <= 0):
                    raise ValidationError("stage sla_hours must be positive")
                expected_duration_hours = stage.get("expected_duration_hours")
                if expected_duration_hours is not None and (
                    not isinstance(expected_duration_hours, (int, float))
                    or isinstance(expected_duration_hours, bool)
                    or expected_duration_hours <= 0
                ):
                    raise ValidationError("stage expected_duration_hours must be positive")
                stages.append(
                    {
                        "key": stage_key,
                        "name": _required_text(stage.get("name") or stage.get("title"), "stage name"),
                        "sequence": int(stage.get("sequence", stage.get("order", index + 1))),
                        "assignee_wing": _required_text(
                            stage.get("assignee_wing") or stage.get("wing") or stage.get("owner_wing") or assignee.get("wing"),
                            "stage assignee wing",
                        ),
                        "assignee_role": _required_text(
                            stage.get("assignee_role") or stage.get("role") or stage.get("owner_role") or assignee.get("role"),
                            "stage assignee role",
                        ),
                        "assignee_person_id": stage.get("assignee_person_id") or assignee.get("person_id") or assignee.get("person"),
                        "assignee_principal_type": str(stage.get("assignee_principal_type") or assignee.get("principal_type") or ("agent" if stage.get("assignee_agent_id") or assignee.get("agent_id") else "person")).lower(),
                        "assignee_principal_id": stage.get("assignee_agent_id") or assignee.get("agent_id") or stage.get("assignee_person_id") or assignee.get("person_id") or assignee.get("person"),
                        "required_evidence": normalized_evidence,
                        "requires_approval": bool(requires_approval),
                        "dependencies": [_required_text(item, "dependency stage key") for item in dependencies],
                        "handoff_to_wing": handoff_to_wing,
                        "handoff_to_role": handoff_to_role,
                        "handoff_to_person_id": handoff_to_person_id,
                        "handoff_principal_type": str(handoff.get("principal_type") or ("agent" if handoff.get("agent_id") else "person")).lower(),
                        "handoff_principal_id": handoff.get("agent_id") or handoff_to_person_id,
                        "handoff_structured": bool(structured_handoff_wing and structured_handoff_role),
                        "handoff_contract": handoff_contract,
                        "on_reject_stage_key": on_reject_stage_key,
                        "due_at": _iso(stage.get("due_at") or stage.get("deadline")),
                        "sla_hours": sla_hours,
                        "expected_duration_hours": expected_duration_hours,
                    }
                )
            stages.sort(key=lambda item: (item["sequence"], item["key"]))
            self._validate_dependencies(stages)
            edges = [
                {"from": dependency, "to": stage["key"], "kind": "depends_on"}
                for stage in stages
                for dependency in stage["dependencies"]
            ]
            return {"key": key, "name": name, "version": version, "stages": stages, "edges": edges}

    def _validate_dependencies(self, stages: list[dict[str, Any]]) -> None:
            stage_keys = {stage["key"] for stage in stages}
            by_sequence = {stage["key"]: stage["sequence"] for stage in stages}
            for stage in stages:
                unknown = [item for item in stage["dependencies"] if item not in stage_keys]
                if unknown:
                    raise ValidationError(f"unknown workflow stage dependency: {', '.join(unknown)}")
                target = stage.get("on_reject_stage_key")
                if target is not None:
                    if target not in stage_keys:
                        raise ValidationError(f"unknown workflow rejection target: {target}")
                    if by_sequence[target] >= stage["sequence"]:
                        raise ValidationError("workflow rejection targets must be earlier stages")
            visiting: set[str] = set()
            visited: set[str] = set()
            by_key = {stage["key"]: stage for stage in stages}
    
            def visit(key: str) -> None:
                if key in visited:
                    return
                if key in visiting:
                    raise ValidationError("workflow stage dependencies cannot form a cycle")
                visiting.add(key)
                for dependency in by_key[key]["dependencies"]:
                    visit(dependency)
                visiting.remove(key)
                visited.add(key)
    
            for key in stage_keys:
                visit(key)

    def _active_client_roster(
            self, organization_id: str, workspace_id: str | None, as_of: str
        ) -> dict[str, Any] | None:
            """Return the latest effective roster for a client workspace, if any."""
            if not workspace_id:
                return None
            row = self.conn.execute(
                """
                SELECT * FROM client_account_rosters
                WHERE organization_id=? AND workspace_id=? AND effective_at<=?
                ORDER BY effective_at DESC, created_at DESC, id DESC LIMIT 1
                """,
                (organization_id, workspace_id, as_of),
            ).fetchone()
            if row is None:
                return None
            row_dict = dict(row)
            version = row_dict["version"]
            roles = [
                dict(item)
                for item in self.conn.execute(
                    """
                    SELECT id, roster_id, organization_id, workspace_id, role_key, wing, person_id, principal_type, agent_id
                    FROM client_account_roster_roles
                    WHERE roster_id=? AND organization_id=? AND workspace_id=?
                    ORDER BY role_key, wing, id
                    """,
                    (row_dict["id"], organization_id, workspace_id),
                ).fetchall()
            ]
            return {
                "id": row_dict["id"],
                "organization_id": row_dict["organization_id"],
                "workspace_id": row_dict["workspace_id"],
                "version": version,
                "roles": roles,
            }

    @staticmethod
    def _roster_role_key(label: Any) -> str:
            text = "" if label is None else str(label).strip().casefold()
            if "account" in text:
                return "account_lead" if "lead" in text else "account_executive"
            if "lead" in text:
                return "wing_lead"
            return "wing_executive"

    @staticmethod
    def _wing_key(value: Any) -> str:
            return "" if value is None else str(value).strip().casefold()

    def _matching_roster_rows(
            self, roster: dict[str, Any], role_label: Any, wing: Any
        ) -> list[dict[str, Any]]:
            role_key = self._roster_role_key(role_label)
            rows = [row for row in roster["roles"] if row.get("role_key") == role_key]
            # Account roles are account-wide and intentionally have no wing.
            if role_key not in {"account_lead", "account_executive"}:
                wing_key = self._wing_key(wing)
                rows = [row for row in rows if self._wing_key(row.get("wing")) == wing_key]
            return rows

    def _require_eligible_owner(self, organization_id: str, workspace_id: str | None, person_id: Any, label: str) -> str:
            resolved_person_id = _required_text(person_id, label)
            if workspace_id is None:
                raise ValidationError(f"{label} requires a workspace-scoped active roster owner")
            row = self.conn.execute(
                """SELECT om.role AS organization_role, wm.role AS workspace_role
                   FROM people p
                   JOIN organization_memberships om
                     ON om.person_id=p.id AND om.organization_id=p.organization_id
                   JOIN workspace_memberships wm ON wm.person_id=p.id
                  WHERE p.organization_id=? AND p.id=? AND p.status='active' AND wm.workspace_id=?""",
                (organization_id, resolved_person_id, workspace_id),
            ).fetchone()
            if row is None:
                raise ValidationError(f"{label} must be an active workspace member")
            capabilities = role_capabilities(str(row["organization_role"]), str(row["workspace_role"]))
            if "workflow_run" not in capabilities:
                raise ValidationError(f"{label} must have workflow_run capability")
            return resolved_person_id

    def _require_eligible_principal(self, organization_id: str, workspace_id: str | None, principal_type: str, principal_id: Any, label: str) -> str:
            if principal_type == "person":
                return self._require_eligible_owner(organization_id, workspace_id, principal_id, label)
            resolved = _required_text(principal_id, label)
            row = self.conn.execute("SELECT status,allowed_workspace_ids,capability_tags FROM agents WHERE organization_id=? AND id=?", (organization_id, resolved)).fetchone()
            if workspace_id is None or row is None or row["status"] not in {"idle", "running"}:
                raise ValidationError(f"{label} must be an active workspace-eligible agent")
            try:
                allowed = set(json.loads(row["allowed_workspace_ids"] or "[]")); capabilities = set(json.loads(row["capability_tags"] or "[]"))
            except (TypeError, ValueError):
                allowed, capabilities = set(), set()
            if workspace_id not in {str(item) for item in allowed} or "workflow_run" not in capabilities:
                raise ValidationError(f"{label} must be workspace-eligible with workflow_run capability")
            return resolved

    def _ensure_stage_has_named_owner(
            self, run: dict[str, Any], stage: dict[str, Any], organization_id: str, workspace_id: str | None
        ) -> None:
            snapshot = run.get("template_snapshot") or {}
            if not snapshot.get("client_roster_id"):
                raise ValidationError("workflow stage cannot start without an active client roster owner")
            self._require_eligible_principal(
                organization_id,
                workspace_id,
                stage.get("assignee_principal_type", "person"),
                stage.get("assignee_principal_id") or stage.get("assignee_person_id"),
                f"stage {stage['stage_key']} owner",
            )

    def _resolve_roster_assignments(self, snapshot: dict[str, Any], roster: dict[str, Any]) -> None:
            """Resolve missing stage/handoff people against one immutable roster.
    
            Every failure occurs before definition/run persistence, preserving the
            all-or-nothing create_run contract.
            """
            for stage in snapshot["stages"]:
                matches = self._matching_roster_rows(roster, stage["assignee_role"], stage["assignee_wing"])
                explicit = stage.get("assignee_principal_id") or stage.get("assignee_person_id")
                if explicit:
                    expected_id = matches[0].get("agent_id") if matches and matches[0].get("principal_type") == "agent" else (matches[0]["person_id"] if matches else None)
                    if len(matches) != 1 or expected_id != explicit:
                        raise ValidationError(
                            f"explicit assignee for stage {stage['key']} does not match active client roster"
                        )
                else:
                    if len(matches) != 1:
                        raise ValidationError(
                            f"active client roster has {len(matches)} matches for stage {stage['key']}"
                        )
                    stage["assignee_principal_id"] = matches[0].get("agent_id") if (matches[0].get("principal_type") == "agent") else matches[0]["person_id"]
                    stage["assignee_principal_type"] = matches[0].get("principal_type") or "person"
                stage["assignee_principal_id"] = self._require_eligible_principal(
                    roster["organization_id"],
                    roster["workspace_id"],
                    stage.get("assignee_principal_type", "person"), stage["assignee_principal_id"],
                    f"stage {stage['key']} owner",
                )
                stage["assignee_person_id"] = stage["assignee_principal_id"] if stage.get("assignee_principal_type") == "person" else None
    
                # Only structured handoffs can be roster-resolved. Opaque legacy
                # handoff_target values remain untouched and never drive guessing.
                if stage.get("handoff_structured") and not stage.get("handoff_principal_id") and not stage.get("handoff_to_person_id"):
                    handoff_matches = self._matching_roster_rows(
                        roster, stage.get("handoff_to_role"), stage.get("handoff_to_wing")
                    )
                    if len(handoff_matches) != 1:
                        raise ValidationError(
                            f"active client roster has {len(handoff_matches)} matches for handoff from stage {stage['key']}"
                        )
                    stage["handoff_principal_type"] = handoff_matches[0].get("principal_type") or "person"
                    stage["handoff_principal_id"] = handoff_matches[0].get("agent_id") if stage["handoff_principal_type"] == "agent" else handoff_matches[0]["person_id"]
                    stage["handoff_to_person_id"] = stage["handoff_principal_id"] if stage["handoff_principal_type"] == "person" else None
                elif stage.get("handoff_structured") and (stage.get("handoff_principal_id") or stage.get("handoff_to_person_id")):
                    handoff_matches = self._matching_roster_rows(
                        roster, stage.get("handoff_to_role"), stage.get("handoff_to_wing")
                    )
                    expected_id = handoff_matches[0].get("agent_id") if handoff_matches and handoff_matches[0].get("principal_type") == "agent" else (handoff_matches[0]["person_id"] if handoff_matches else None)
                    explicit_handoff = stage.get("handoff_principal_id") or stage.get("handoff_to_person_id")
                    if len(handoff_matches) != 1 or expected_id != explicit_handoff:
                        raise ValidationError(
                            f"explicit handoff person from stage {stage['key']} does not match active client roster"
                        )
                if stage.get("handoff_structured"):
                    stage["handoff_principal_id"] = self._require_eligible_principal(
                        roster["organization_id"],
                        roster["workspace_id"],
                        stage.get("handoff_principal_type", "person"), stage.get("handoff_principal_id") or stage.get("handoff_to_person_id"),
                        f"handoff owner from stage {stage['key']}",
                    )
                    stage["handoff_to_person_id"] = stage["handoff_principal_id"] if stage.get("handoff_principal_type", "person") == "person" else None
