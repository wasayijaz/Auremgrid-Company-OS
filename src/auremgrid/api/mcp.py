from __future__ import annotations

from datetime import datetime
from typing import Any

from auremgrid.domain.errors import AuremgridError, AuthorizationError
from auremgrid.domain.security import AuthenticatedIdentity
from auremgrid.services.brain import CompanyOS


class McpToolRouter:
    """Protocol-neutral handlers that can sit behind MCP or any other agent transport."""

    def __init__(self, os: CompanyOS, identity: AuthenticatedIdentity) -> None:
        self.os = os
        self.identity = identity

    def list_tools(self) -> list[dict[str, Any]]:
        tools = [
            {"name": "search", "description": "Retrieve citation-backed evidence for a workspace query."},
            {"name": "entity", "description": "Return facts and relations for one entity."},
            {"name": "history", "description": "Return temporal versions of a subject/predicate claim."},
            {"name": "neighbors", "description": "Return graph neighbors for an entity."},
            {"name": "sources", "description": "List sources visible to the actor."},
            {"name": "recent", "description": "List recently ingested documents visible to the actor."},
            {"name": "remember", "description": "Store an actor-scoped preference or interaction note."},
            {"name": "brain.propose", "description": "Create a human-gated brain proposal in the authenticated workspace."},
            {"name": "brain.promote", "description": "Approve or reject a brain proposal in the authenticated workspace."},
            {"name": "brain.resolve_conflict", "description": "Resolve a fact conflict with an authenticated winner."},
            {"name": "brief", "description": "Assemble the client brief: brain, playbooks, open work, and last touchpoint."},
            {"name": "engines", "description": "Show what each open-source engine contributed for a query."},
            {"name": "work", "description": "List open or all work items in a workspace."},
            {"name": "capture_work", "description": "Capture a new request at the front door of the work loop."},
            {"name": "assign_work", "description": "Assign a captured work item to an actor in the same workspace."},
            {"name": "start_work", "description": "Move an assigned work item into production."},
            {"name": "mark_dod", "description": "Update definition-of-done checks for a work item."},
            {"name": "submit_review", "description": "Submit a complete work item for internal review."},
            {"name": "close_review", "description": "Approve or return an item from internal review."},
            {"name": "ship_work", "description": "Ship an item after client review is complete."},
            {"name": "projects.list", "description": "List projects visible to an organization person."},
            {"name": "projects.get", "description": "Get a project without exposing another workspace."},
            {"name": "decisions.list", "description": "List temporal decisions in an allowed workspace."},
            {"name": "decisions.create", "description": "Create a durable, sourced decision."},
            {"name": "people.list", "description": "List organization people for an authorized member."},
            {"name": "clients.list", "description": "List client workspaces visible to a person."},
            {"name": "clients.health", "description": "Calculate explainable client health."},
            {"name": "clients.roster.get", "description": "Get the identity-scoped client account roster."},
            {"name": "clients.roster.create", "description": "Create an append-only client account roster."},
            {"name": "meetings.list", "description": "List meetings in an allowed client workspace."},
            {"name": "meetings.responsibilities.get", "description": "Get facilitator and note-taker responsibilities for a meeting."},
            {"name": "meetings.responsibilities.set", "description": "Set facilitator and note-taker responsibilities for a meeting."},
            {"name": "campaigns.list", "description": "List campaigns in an allowed workspace."},
            {"name": "campaigns.performance", "description": "Return sourced campaign performance or not_connected."},
            {"name": "people.capacity", "description": "Return capacity snapshots for the organization."},
            {"name": "risks.list", "description": "List open client risks."},
            {"name": "opportunities.list", "description": "List client opportunities."},
            {"name": "agents.list", "description": "Return agents and recent auditable runs."},
            {"name": "notifications.list", "description": "Return relevance-ranked attention items."},
            {"name": "reports.generate", "description": "Generate a report with canonical citations."},
            {"name": "workflows.templates", "description": "List validated cross-wing workflow templates."},
            {"name": "workflows.runs.get", "description": "Get a workflow run, stages, progress, and audit history."},
            {"name": "workflows.runs.create", "description": "Start an immutable run from a validated workflow template."},
            {"name": "workflows.stages.start", "description": "Start a dependency-ready workflow stage."},
            {"name": "workflows.stages.complete", "description": "Complete a stage after evidence and approval gates pass."},
            {"name": "workflows.evidence.add", "description": "Attach canonical or locator-backed evidence to a workflow stage."},
            {"name": "workflows.approvals.request", "description": "Move an evidence-complete workflow stage to approval."},
            {"name": "workflows.approvals.decide", "description": "Approve or return a gated workflow stage with a reason."},
            {"name": "workflows.handoffs.acknowledge", "description": "Accept a cross-wing artifact handoff contract."},
            {"name": "integrations.list", "description": "List sanitized integration connection and sync state."},
            {"name": "integrations.configure", "description": "Configure explicit provider-container to workspace mappings."},
            {"name": "integrations.credentials.bind", "description": "Bind an external secret reference to an integration."},
            {"name": "integrations.verify", "description": "Verify a bound credential with its provider."},
            {"name": "integrations.sync", "description": "Enqueue durable synchronization for a verified integration."},
        ]
        namespaced = {
            "brain.search":"Search cited evidence.","brain.entity":"Get a brain entity.","brain.history":"Get temporal fact history.",
            "brain.neighbors":"Get entity neighbors.","brain.sources":"List permitted sources.","brain.recent":"List recent evidence.",
            "clients.brief":"Get a client operating brief.","work.list":"List work.","work.create":"Capture work.",
            "work.assign":"Assign work.","work.update":"Advance work.","work.review":"Submit work for review.",
            "meetings.get":"Get one meeting.","campaigns.get":"Get one campaign.","agents.runs":"List auditable agent runs.",
        }
        existing={tool["name"] for tool in tools}
        tools.extend({"name":name,"description":description} for name,description in namespaced.items() if name not in existing)
        return tools

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            if not isinstance(arguments, dict):
                raise AuremgridError("arguments must be an object")
            aliases={"brain.search":"search","brain.entity":"entity","brain.history":"history","brain.neighbors":"neighbors",
                "brain.sources":"sources","brain.recent":"recent","clients.brief":"brief","work.list":"work",
                "work.create":"capture_work","work.assign":"assign_work","work.update":"start_work","work.review":"submit_review"}
            name=aliases.get(name,name)
            arguments = self._trusted_arguments(name, arguments)
            company_tools={"projects.list","projects.get","decisions.list","decisions.create","people.list","clients.list",
                "clients.health","clients.roster.get","clients.roster.create","meetings.list","meetings.get",
                "meetings.responsibilities.get","meetings.responsibilities.set","campaigns.list","campaigns.get","campaigns.performance",
                "people.capacity","risks.list","opportunities.list","agents.list","agents.runs","notifications.list","reports.generate",
                "workflows.templates","workflows.runs.get","workflows.runs.create","workflows.stages.start",
                "workflows.stages.complete","workflows.evidence.add","workflows.approvals.request",
                "workflows.approvals.decide","workflows.handoffs.acknowledge","integrations.list",
                "integrations.configure","integrations.credentials.bind","integrations.verify","integrations.sync"}
            if name in company_tools:
                return self._call_company_tool(name, arguments)
            workspace_id = _required(arguments, "workspace_id")
            actor_id = _optional_str(arguments.get("actor_id"))
            if name == "search":
                actor_id = _required(arguments, "actor_id")
                as_of = _optional_dt(arguments.get("as_of"))
                return self.os.search(
                    workspace_id,
                    actor_id,
                    _required(arguments, "query"),
                    as_of=as_of,
                    limit=int(arguments.get("limit", 8)),
                ).to_dict()
            if name == "entity":
                actor_id = _required(arguments, "actor_id")
                return self.os.entity(
                    workspace_id,
                    actor_id,
                    _required(arguments, "name"),
                    as_of=_optional_dt(arguments.get("as_of")),
                )
            if name == "history":
                actor_id = _required(arguments, "actor_id")
                return self.os.history(
                    workspace_id,
                    actor_id,
                    _required(arguments, "subject"),
                    predicate=arguments.get("predicate"),
                )
            if name == "neighbors":
                actor_id = _required(arguments, "actor_id")
                return self.os.neighbors(
                    workspace_id,
                    actor_id,
                    _required(arguments, "entity"),
                    as_of=_optional_dt(arguments.get("as_of")),
                )
            if name == "sources":
                actor_id = _required(arguments, "actor_id")
                return self.os.sources(workspace_id, actor_id)
            if name == "recent":
                actor_id = _required(arguments, "actor_id")
                return self.os.recent(workspace_id, actor_id, limit=int(arguments.get("limit", 5)))
            if name == "remember":
                actor_id = _required(arguments, "actor_id")
                memory = self.os.remember(
                    workspace_id,
                    actor_id,
                    _required(arguments, "content"),
                    kind=str(arguments.get("kind", "preference")),
                )
                return memory.to_dict()
            if name == "brain.propose":
                kind = _required(arguments, "kind")
                if kind in {"memory", "fact", "decision"}:
                    return self.os.brain_ops.create_proposal(
                        self.identity.organization_id, workspace_id, "person", self.identity,
                        kind, _required(arguments, "content"), arguments.get("payload") or {},
                        _required(arguments, "evidence"), float(arguments.get("confidence", 0.5)),
                        _optional_str(arguments.get("source_id")),
                    )
                return self.os.brain_ops.brain_propose(
                    self.identity.organization_id, workspace_id, self.identity,
                    kind, [str(item) for item in arguments.get("candidate_entity_ids", [])],
                    float(arguments.get("score", 0.0)), _required(arguments, "rationale"),
                    _required(arguments, "evidence"), _optional_str(arguments.get("alias")),
                    _optional_str(arguments.get("source_id")), _optional_str(arguments.get("target_id")),
                    arguments.get("evidence_refs") or {},
                )
            if name == "brain.promote":
                proposal_id, action = _required(arguments, "proposal_id"), _required(arguments, "action")
                row = self.os.store.conn.execute("SELECT kind FROM memory_proposals WHERE organization_id=? AND id=?",
                    (self.identity.organization_id, proposal_id)).fetchone()
                if row is not None:
                    return self.os.brain_ops.brain_promote_fact(self.identity, proposal_id, action)
                return self.os.brain_ops.brain_promote(self.identity.organization_id, workspace_id, self.identity, proposal_id, action)
            if name == "brain.resolve_conflict":
                return self.os.brain_ops.resolve_fact_conflict(self.identity, _required(arguments, "conflict_group"), _required(arguments, "winner_fact_id"))
            if name == "brief":
                actor_id = _required(arguments, "actor_id")
                return self.os.account_brief(
                    workspace_id,
                    actor_id,
                    query=arguments.get("query"),
                ).to_dict()
            if name == "work":
                actor_id = _required(arguments, "actor_id")
                items = self.os.list_work(
                    workspace_id,
                    actor_id,
                    open_only=bool(arguments.get("open_only", True)),
                )
                return {"work": [item.to_dict() for item in items]}
            if name == "engines":
                return self.os.engine_status(
                    workspace_id,
                    actor_id,
                    _required(arguments, "query"),
                )
            if name == "capture_work":
                return self.os.capture_work(
                    workspace_id,
                    actor_id,
                    _required(arguments, "title"),
                    _required(arguments, "request"),
                    _required(arguments, "requested_by"),
                    needed_by=_optional_str(arguments.get("needed_by")),
                    playbook_id=_optional_str(arguments.get("playbook_id")),
                    decision_maker=_optional_str(arguments.get("decision_maker")),
                ).to_dict()
            if name == "assign_work":
                return self.os.assign_work(
                    workspace_id,
                    actor_id,
                    _required(arguments, "work_item_id"),
                    _required(arguments, "assignee_id"),
                    decision_maker=_optional_str(arguments.get("decision_maker")),
                ).to_dict()
            if name == "start_work":
                return self.os.start_work(
                    workspace_id,
                    actor_id,
                    _required(arguments, "work_item_id"),
                ).to_dict()
            if name == "mark_dod":
                checks = arguments.get("checks")
                if not isinstance(checks, dict):
                    raise AuremgridError("checks must be an object")
                return self.os.mark_dod(
                    workspace_id,
                    actor_id,
                    _required(arguments, "work_item_id"),
                    {str(key): _bool(value, f"checks.{key}") for key, value in checks.items()},
                ).to_dict()
            if name == "submit_review":
                return self.os.submit_review(
                    workspace_id,
                    actor_id,
                    _required(arguments, "work_item_id"),
                ).to_dict()
            if name == "close_review":
                return self.os.close_review(
                    workspace_id,
                    actor_id,
                    _required(arguments, "work_item_id"),
                    _bool(arguments.get("approved"), "approved"),
                    note=str(arguments.get("note", "")),
                ).to_dict()
            if name == "ship_work":
                return self.os.ship_work(
                    workspace_id,
                    actor_id,
                    _required(arguments, "work_item_id"),
                    note=str(arguments.get("note", "")),
                ).to_dict()
            raise AuremgridError(f"unknown tool: {name}")
        except (AuremgridError, ValueError, TypeError) as exc:
            return {"error": exc.__class__.__name__, "message": str(exc)}

    def _trusted_arguments(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = dict(arguments)
        supplied_org = _optional_str(result.get("organization_id"))
        if supplied_org and supplied_org != self.identity.organization_id:
            raise AuthorizationError("identity organization mismatch")
        supplied_person = _optional_str(result.get("person_id"))
        if supplied_person and supplied_person != self.identity.person_id:
            raise AuthorizationError("identity person mismatch")
        result["organization_id"] = self.identity.organization_id
        result["person_id"] = self.identity.person_id
        workspace_id = _optional_str(result.get("workspace_id"))
        if workspace_id:
            if self.identity.workspace_id != workspace_id:
                raise AuthorizationError("identity workspace mismatch")
        capability = _mcp_capability(name)
        self.identity.require(capability)
        if name in {"search","entity","history","neighbors","sources","recent","remember","brief","work",
            "capture_work","assign_work","start_work","mark_dod","submit_review","close_review","ship_work"}:
            workspace = _required(result, "workspace_id")
            actor_id = self.os.auth.actor_for_identity(self.identity, workspace)
            supplied_actor = _optional_str(result.get("actor_id"))
            if supplied_actor and supplied_actor != actor_id:
                raise AuthorizationError("identity actor mismatch")
            result["actor_id"] = actor_id
        return result

    def _call_company_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        organization_id = _required(arguments, "organization_id")
        person_id = _required(arguments, "person_id")
        workspace_id = _optional_str(arguments.get("workspace_id"))
        if name == "projects.list":
            items = self.os.list_projects(organization_id, _required(arguments, "workspace_id"), person_id)
            return {"projects": [item.to_dict() for item in items]}
        if name == "projects.get":
            workspace = _required(arguments, "workspace_id")
            self.os._require_person_access(organization_id, workspace, person_id)
            item = self.os.company.get_project(workspace, _required(arguments, "project_id"))
            if item is None:
                raise AuremgridError("project not found")
            return item.to_dict()
        if name == "decisions.list":
            if workspace_id:
                self.os._require_person_access(organization_id, workspace_id, person_id)
            elif self.os.company.org_membership(organization_id, person_id) is None:
                raise AuremgridError("person is not an organization member")
            return {"decisions": [item.to_dict() for item in self.os.company.list_decisions(organization_id, workspace_id)]}
        if name == "decisions.create":
            return self.os.create_decision(
                organization_id, person_id, _required(arguments, "statement"), _required(arguments, "rationale"),
                workspace_id, _optional_str(arguments.get("project_id")), _optional_str(arguments.get("source_id")),
                str(arguments.get("evidence", "")), [str(value) for value in arguments.get("tags", [])],
            ).to_dict()
        if name == "people.list":
            if self.os.company.org_membership(organization_id, person_id) is None:
                raise AuremgridError("person is not an organization member")
            return {"people": [item.to_dict() for item in self.os.company.list_people(organization_id)]}
        if name == "clients.list":
            items=self.os.company.list_workspaces(organization_id)
            visible=[item for item in items if item["kind"]=="client" and self.os.company.workspace_membership(item["id"],person_id)]
            return {"clients":visible}
        if name == "clients.health":
            return self.os.client_ops.calculate_health(organization_id,_required(arguments,"workspace_id"),person_id).to_dict()
        if name in {"clients.roster.get", "clients.roster.create"}:
            workspace = _required(arguments, "workspace_id")
            self.os._require_person_access(organization_id, workspace, person_id)
            if name == "clients.roster.get":
                result = self.os.client_ops.get_client_roster(
                    organization_id, workspace, person_id, _optional_str(arguments.get("roster_id")),
                    as_of=_optional_dt(arguments.get("as_of")),
                )
                if result is None:
                    raise AuremgridError("client roster not found")
                return result
            return self.os.client_ops.create_client_roster(
                organization_id, workspace, person_id, arguments.get("roles") or [],
                _optional_str(arguments.get("effective_at")), str(arguments.get("note", "")),
            )
        if name in {"meetings.list","meetings.get"}:
            workspace=_required(arguments,"workspace_id"); self.os._require_person_access(organization_id,workspace,person_id)
            if name.endswith(".get"):
                row=self.os.store.conn.execute("SELECT * FROM meetings WHERE workspace_id=? AND id=?",(workspace,_required(arguments,"meeting_id"))).fetchone()
                if row is None: raise AuremgridError("meeting not found")
                return dict(row)
            return {"meetings":[dict(r) for r in self.os.store.conn.execute("SELECT * FROM meetings WHERE workspace_id=? ORDER BY occurred_at DESC",(workspace,)).fetchall()]}
        if name in {"meetings.responsibilities.get", "meetings.responsibilities.set"}:
            workspace = _required(arguments, "workspace_id")
            self.os._require_person_access(organization_id, workspace, person_id)
            meeting_id = _required(arguments, "meeting_id")
            if name.endswith(".get"):
                return self.os.client_ops.get_meeting_responsibilities(
                    organization_id, workspace, person_id, meeting_id, as_of=_optional_dt(arguments.get("as_of")),
                )
            return self.os.client_ops.set_meeting_responsibilities(
                organization_id, workspace, person_id, meeting_id,
                facilitator_person_id=_optional_str(arguments.get("facilitator_person_id")),
                note_taker_person_id=_optional_str(arguments.get("note_taker_person_id")),
                reason=str(arguments.get("reason", "manual")),
            )
        if name in {"campaigns.list","campaigns.get","campaigns.performance"}:
            workspace=_required(arguments,"workspace_id"); self.os._require_person_access(organization_id,workspace,person_id)
            if name=="campaigns.list": return {"campaigns":[dict(r) for r in self.os.store.conn.execute("SELECT * FROM campaigns WHERE workspace_id=? ORDER BY updated_at DESC",(workspace,)).fetchall()]}
            campaign_id=_required(arguments,"campaign_id")
            if name=="campaigns.performance": return self.os.agency_ops.campaign_performance(organization_id,workspace,person_id,campaign_id)
            row=self.os.store.conn.execute("SELECT * FROM campaigns WHERE workspace_id=? AND id=?",(workspace,campaign_id)).fetchone()
            if row is None: raise AuremgridError("campaign not found")
            return dict(row)
        if name == "people.capacity":
            if self.os.company.org_membership(organization_id,person_id) is None: raise AuremgridError("organization membership required")
            return {"capacity":[dict(r) for r in self.os.store.conn.execute("SELECT * FROM capacity_snapshots WHERE organization_id=? ORDER BY calculated_at DESC",(organization_id,)).fetchall()]}
        if name in {"risks.list","opportunities.list"}:
            workspace=_required(arguments,"workspace_id"); self.os._require_person_access(organization_id,workspace,person_id)
            if name=="risks.list": return {"risks":self.os.client_ops.list_risks(organization_id,workspace,person_id)}
            return {"opportunities":[dict(r) for r in self.os.store.conn.execute("SELECT * FROM opportunities WHERE workspace_id=? ORDER BY created_at DESC",(workspace,)).fetchall()]}
        if name in {"agents.list","agents.runs"}: return self.os.agent_ops.command_center(organization_id,person_id)
        if name == "notifications.list": return {"notifications":self.os.agency_ops.attention(organization_id,person_id,int(arguments.get("limit",20)))}
        if name == "reports.generate": return self.os.agent_ops.generate_report(organization_id,person_id,_required(arguments,"type"),workspace_id)
        if name == "workflows.templates":
            templates=self.os.workflow_catalog.for_wing(str(arguments["wing"])) if arguments.get("wing") else self.os.workflow_catalog.all()
            return {"templates":[item.to_dict() for item in templates]}
        if name == "workflows.runs.get":
            return self.os.workflow_ops.summary(organization_id,_required(arguments,"workspace_id"),person_id,_required(arguments,"run_id"))
        if name == "workflows.runs.create":
            template=self.os.workflow_catalog.get(_required(arguments,"template_id"))
            return self.os.workflow_ops.create_run(organization_id,_required(arguments,"workspace_id"),person_id,template,
                _optional_str(arguments.get("due_at")),_optional_int(arguments.get("sla_minutes")),_optional_str(arguments.get("idempotency_key")))
        if name == "workflows.stages.start":
            return self.os.workflow_ops.start_stage(organization_id,_required(arguments,"workspace_id"),person_id,
                _required(arguments,"run_id"),_required(arguments,"stage_id"),_optional_int(arguments.get("expected_version")),
                _optional_str(arguments.get("idempotency_key")))
        if name == "workflows.stages.complete":
            return self.os.workflow_ops.complete_stage(organization_id,_required(arguments,"workspace_id"),person_id,
                _required(arguments,"run_id"),_required(arguments,"stage_id"),str(arguments.get("reason","")),
                _optional_int(arguments.get("expected_version")),_optional_str(arguments.get("idempotency_key")))
        if name == "workflows.evidence.add":
            return self.os.workflow_ops.submit_evidence(organization_id,_required(arguments,"workspace_id"),person_id,
                _required(arguments,"run_id"),_required(arguments,"stage_id"),_required(arguments,"kind"),
                _optional_str(arguments.get("uri")),_optional_str(arguments.get("text")),arguments.get("metadata") or {},
                _optional_str(arguments.get("object_type")),_optional_str(arguments.get("object_id")),
                _optional_str(arguments.get("locator")),_optional_str(arguments.get("content_hash")),
                _optional_str(arguments.get("idempotency_key")))
        if name == "workflows.approvals.request":
            return self.os.workflow_ops.request_approval(organization_id,_required(arguments,"workspace_id"),person_id,
                _required(arguments,"run_id"),_required(arguments,"stage_id"),_required(arguments,"reason"),
                _optional_str(arguments.get("approval_request_id")),_optional_int(arguments.get("expected_version")),
                _optional_str(arguments.get("idempotency_key")))
        if name == "workflows.approvals.decide":
            return self.os.workflow_ops.decide_approval(organization_id,_required(arguments,"workspace_id"),person_id,
                _required(arguments,"run_id"),_required(arguments,"stage_id"),_required(arguments,"decision"),
                _required(arguments,"reason"),_optional_str(arguments.get("approval_request_id")),
                _optional_str(arguments.get("idempotency_key")))
        if name == "workflows.handoffs.acknowledge":
            return self.os.workflow_ops.acknowledge_handoff(organization_id,_required(arguments,"workspace_id"),person_id,
                _required(arguments,"run_id"),_required(arguments,"from_stage_id"),_required(arguments,"to_stage_id"),
                _required(arguments,"artifact_contract"),str(arguments.get("reason","")),_optional_str(arguments.get("idempotency_key")))
        if name == "integrations.list":
            return {"integrations": self.os.integrations.list(self.identity)}
        if name == "integrations.configure":
            mappings=arguments.get("workspace_mappings") or {}
            if not isinstance(mappings,dict): raise AuremgridError("workspace_mappings must be an object")
            return self.os.integrations.configure(self.identity,_required(arguments,"source"),_required(arguments,"expected_account_id"),
                {str(key):str(value) for key,value in mappings.items()},
                [str(value) for value in arguments.get("permissions",[])])
        if name == "integrations.credentials.bind":
            return self.os.integrations.bind_credential(self.identity,_required(arguments,"integration_id"),
                _required(arguments,"name"),_required(arguments,"reference"),
                [str(value) for value in arguments.get("scopes",[])])
        if name == "integrations.verify":
            return self.os.integrations.verify(self.identity,_required(arguments,"integration_id"))
        if name == "integrations.sync":
            integration_id=_required(arguments,"integration_id")
            return {"jobs":self.os.integrations.enqueue_sync(self.identity,integration_id,
                int(arguments.get("priority",0)),int(arguments.get("max_attempts",5)),
                _optional_str(arguments.get("idempotency_key")))}
        raise AuremgridError(f"unknown tool: {name}")


def _required(arguments: dict[str, Any], key: str) -> str:
    value = arguments.get(key)
    if not value:
        raise AuremgridError(f"{key} is required")
    return str(value)


def _optional_dt(value: Any) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(str(value))


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _optional_int(value: Any) -> int | None:
    return int(value) if value is not None else None


def _mcp_capability(name: str) -> str:
    if name in {"brain.propose"}: return "brain_propose"
    if name in {"brain.promote"}: return "brain_promote"
    if name in {"brain.resolve_conflict"}: return "brain_promote"
    if name in {"search","entity","history","neighbors","sources","recent","brief","engines"} or name.startswith("brain."):
        return "brain_read"
    if name == "remember": return "brain_propose"
    if name in {"decisions.create"}: return "brain_promote"
    if name in {"agents.list","agents.runs"}: return "agent_run"
    if name == "reports.generate": return "workspace_write"
    if name in {"clients.roster.create", "meetings.responsibilities.set"}: return "people_manage"
    if name in {"integrations.list","integrations.configure"}: return "integration_configure"
    if name == "integrations.credentials.bind": return "secret_bind"
    if name in {"integrations.verify","integrations.sync"}: return "integration_sync"
    if name.startswith("workflows.approvals") or name.startswith("workflows.handoffs") or name.startswith("workflows.stages"):
        return "workflow_gate"
    if name == "workflows.runs.create": return "workflow_run"
    if name.startswith("workflows."): return "workspace_read"
    if name in {"capture_work","assign_work","start_work","mark_dod","submit_review","close_review","ship_work",
        "work.create","work.assign","work.update","work.review"}:
        return "workspace_write"
    return "workspace_read"


def _bool(value: Any, key: str) -> bool:
    if isinstance(value, bool):
        return value
    raise AuremgridError(f"{key} must be a boolean")
