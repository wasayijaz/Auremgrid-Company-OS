"""Agent configuration, roles, capability levels, and visibility mixin."""
from __future__ import annotations

import json
from typing import Any, Sequence

from auremgrid.domain.errors import AuthorizationError, NotFoundError, ValidationError
from auremgrid.domain.models import (
    AGENT_LEVEL_ORDER,
    CAPABILITY_LEVELS,
    LEVEL_DEFINITIONS,
    AgentLevel,
    effective_capability_tags,
    normalize_agent_level,
)
from auremgrid.services.agent_ops_shared import (
    MAX_AGENT_DELEGATION_DEPTH,
    _json,
    _now,
)


class AgentOpsConfigMixin:
    @staticmethod
    def _can(capabilities: Any, capability: str) -> bool:
        return capabilities is None or capability in set(capabilities)

    def visible_workspace_ids(self, organization_id: str, person_id: str) -> set[str]:
        """Return only workspaces in *organization_id* that this person belongs to.

        Workspace memberships are organization-scoped through ``workspace_organization``;
        joining that table here prevents a person who belongs to workspaces in another
        organization from accidentally widening an agent response.
        """
        rows = self.conn.execute(
            """SELECT wm.workspace_id
               FROM workspace_memberships wm
               JOIN workspace_organization wo ON wo.workspace_id=wm.workspace_id
               WHERE wm.person_id=? AND wo.organization_id=?""",
            (person_id, organization_id),
        ).fetchall()
        return {str(row[0]) for row in rows}

    @staticmethod
    def _visible_workspace_clause(column: str, visible: set[str]) -> tuple[str, list[Any]]:
        if not visible:
            return f"{column} IS NULL", []
        marks = ",".join("?" for _ in visible)
        return f"({column} IS NULL OR {column} IN ({marks}))", sorted(visible)

    @staticmethod
    def _redacted_agent(row: Any, visible: set[str]) -> dict[str, Any]:
        agent = dict(row)
        try:
            allowed = json.loads(agent.get("allowed_workspace_ids") or "[]")
        except (TypeError, ValueError):
            allowed = []
        agent["allowed_workspace_ids"] = _json([str(item) for item in allowed if str(item) in visible])
        return agent

    def seed_primary_agents(self, organization_id: str, owner_person_id: str) -> list[dict[str, Any]]:
        if self.company.org_membership(organization_id, owner_person_id) is None:
            raise AuthorizationError("organization membership required")
        definitions = (
            ("Sol", "strategic_reviewer", "Review strategy, architecture, and risk", [], AgentLevel.L3_REASON),
            ("Terra", "builder", "Implement and verify deep product work", ["domain.write", "code.write"], AgentLevel.L2_BUILD),
            ("Luna", "operator", "Execute operations and consistency work", ["domain.write"], AgentLevel.L1_OPERATE),
        )
        agents = []
        for name, role, description, writes, level in definitions:
            existing = self.conn.execute(
                "SELECT * FROM agents WHERE organization_id=? AND name=?",
                (organization_id, name),
            ).fetchone()
            if existing:
                capability_tags = _json(list(effective_capability_tags(level)))
                if existing["level"] != level.value or existing["capability_tags"] != capability_tags:
                    self.conn.execute(
                        "UPDATE agents SET level=?,capability_tags=? WHERE organization_id=? AND id=?",
                        (level.value, capability_tags, organization_id, existing["id"]),
                    )
                    existing = self.conn.execute(
                        "SELECT * FROM agents WHERE organization_id=? AND id=?",
                        (organization_id, existing["id"]),
                    ).fetchone()
                agents.append(dict(existing))
                continue
            role_id = self.new_id("agentrole")
            self.conn.execute(
                """INSERT INTO agent_roles(
                    id,organization_id,name,description,default_tools,default_write_permissions
                ) VALUES (?,?,?,?,?,?)""",
                (role_id, organization_id, role, description, _json([]), _json(writes)),
            )
            item = {
                "id": self.new_id("agent"),
                "organization_id": organization_id,
                "name": name,
                "role_id": role_id,
                "model": "unconfigured",
                "tools": _json([]),
                "allowed_workspace_ids": _json([]),
                "memory_access": "proposal_only",
                "write_permissions": _json(writes),
                "level": level.value,
                "capability_tags": _json(list(effective_capability_tags(level))),
                "status": "idle",
                "current_task_id": None,
                "created_at": _now().isoformat(),
            }
            self.conn.execute(
                """INSERT INTO agents(
                    id,organization_id,name,role_id,model,tools,allowed_workspace_ids,memory_access,
                    write_permissions,level,capability_tags,status,current_task_id,created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                tuple(item.values()),
            )
            agents.append(item)
        self.conn.commit()
        return agents

    def configure_agent(
        self,
        organization_id: str,
        owner_person_id: str,
        agent_id: str,
        model: str,
        tools: list[str],
        allowed_workspace_ids: list[str],
        write_permissions: list[str],
    ) -> dict[str, Any]:
        membership = self.company.org_membership(organization_id, owner_person_id)
        if membership is None or membership.role not in {"owner", "admin"}:
            raise AuthorizationError("organization admin required")
        for workspace_id in allowed_workspace_ids:
            scope = self.company.workspace_scope(workspace_id)
            if scope is None or scope["organization_id"] != organization_id:
                raise ValidationError("agent workspace must belong to organization")
        self.conn.execute(
            """UPDATE agents SET model=?,tools=?,allowed_workspace_ids=?,write_permissions=?
            WHERE organization_id=? AND id=?""",
            (
                model,
                _json(tools),
                _json(allowed_workspace_ids),
                _json(write_permissions),
                organization_id,
                agent_id,
            ),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT * FROM agents WHERE organization_id=? AND id=?",
            (organization_id, agent_id),
        ).fetchone()
        if row is None:
            raise NotFoundError("agent not found")
        return dict(row)


    def resolve_level(self, intent_tags: Sequence[str]) -> AgentLevel:
        required_tags = set(self.validate_capability_tags(intent_tags))
        for level in AGENT_LEVEL_ORDER:
            if required_tags.issubset(effective_capability_tags(level)):
                return level
        return AgentLevel.L3_REASON

    def validate_capability_tags(self, intent_tags: Sequence[str]) -> tuple[str, ...]:
        tags = tuple(dict.fromkeys(str(tag).strip() for tag in intent_tags if str(tag).strip()))
        if not tags:
            raise ValidationError("at least one capability tag is required")
        unknown = sorted(tag for tag in tags if tag not in CAPABILITY_LEVELS)
        if unknown:
            raise ValidationError(f"unknown capability tags: {', '.join(unknown)}")
        return tags

    def _selected_level(
        self,
        recommended: AgentLevel,
        selected_level: AgentLevel | str | None,
        override_reason: str,
    ) -> AgentLevel:
        if selected_level is None:
            if override_reason.strip():
                raise ValidationError("level override reason requires a selected level")
            return recommended
        selected = self._normalize_level(selected_level)
        if selected == recommended:
            if override_reason.strip():
                raise ValidationError("level override reason requires a different selected level")
            return selected
        if recommended not in LEVEL_DEFINITIONS[selected].can_handle:
            raise ValidationError("selected level cannot de-escalate below recommended level")
        if not override_reason.strip():
            raise ValidationError("level override reason is required")
        return selected

    @staticmethod
    def _normalize_level(value: AgentLevel | str) -> AgentLevel:
        try:
            return normalize_agent_level(value)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc

    def _validated_delegation_depth(
        self,
        organization_id: str,
        workspace_id: str | None,
        parent_task_id: str | None,
        delegation_depth: int | None,
    ) -> int:
        def parsed(value: int | None) -> int | None:
            if value is None:
                return None
            try:
                return int(value)
            except (TypeError, ValueError) as exc:
                raise ValidationError("delegation depth must be an integer") from exc

        if parent_task_id is None:
            depth = 0 if delegation_depth is None else parsed(delegation_depth)
            if depth != 0:
                raise ValidationError("root agent tasks must use delegation depth 0")
        else:
            parent = self.conn.execute(
                "SELECT organization_id,workspace_id,delegation_depth FROM agent_tasks WHERE organization_id=? AND id=?",
                (organization_id, parent_task_id),
            ).fetchone()
            if parent is None:
                raise NotFoundError("parent agent task not found")
            if parent["workspace_id"] != workspace_id:
                raise AuthorizationError("delegated agent task must stay in the parent workspace")
            depth = int(parent["delegation_depth"]) + 1
            requested_depth = parsed(delegation_depth)
            if requested_depth is not None and requested_depth != depth:
                raise ValidationError("delegation depth must be parent depth plus one")
        if depth < 0 or depth > MAX_AGENT_DELEGATION_DEPTH:
            raise ValidationError("agent delegation depth exceeds configured bound")
        return depth
